"""Narrow host-side finalization of explicitly required research JSON artifacts.

Never uses the model to choose an arbitrary destination, executes no report text,
does not overwrite files, and cannot expand an inspection-only assignment.
This is a worker-authored report, not independent verification.
"""
from __future__ import annotations

import json
import os
import re

REPORT_NAME = re.compile(r"aios-experiment-[0-9a-fA-F-]{36}\.json\Z")


def materialize_research_report(workspace, spec, report):
    """Create only a missing, explicitly expected, root-level experiment JSON file."""
    if getattr(workspace.registry, "tool_mode", None) != "research":
        return []
    if workspace.registry.read_only or spec.inspection_only():
        return []
    try:
        data = json.loads(report)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    if not (isinstance(data.get("objective"), str) and data["objective"].strip()
            and data.get("outcome") in ("completed", "partial", "blocked")
            and isinstance(data.get("evidence"), list)
            and isinstance(data.get("limitations"), list)):
        return []
    # The operator-supplied assignment is authoritative, not the model's
    # paraphrase of it. This prevents a passing file-shape check from silently
    # replacing the task statement with a different objective.
    objective = getattr(spec, "objective", None)
    if isinstance(objective, str) and objective.strip():
        data["objective"] = objective
    # The model can supply additional JSON fields, but cannot choose filenames.
    raw = json.dumps(workspace.clean(data), ensure_ascii=False, indent=2)
    encoded = (raw + "\n").encode("utf-8")
    if len(encoded) > 128 * 1024:
        return []
    created = []
    for output in spec.expected_outputs:
        path = output.path
        if output.kind != "file" or "/" in path or not REPORT_NAME.fullmatch(path):
            continue
        workspace.guard()
        target = workspace.path(path)
        if target.exists() or target.is_symlink():
            continue
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        # O_EXCL prevents clobbering an existing report or following symlinks.
        try:
            fd = os.open(target, flags, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        created.append(path)
    return created
