"""Host-run checks and bounded, content-addressed workspace observations."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

from .assignment import verification_status

IGNORED = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}


def bounded_text(value, budget):
    """Bound encoded JSON size, including non-ASCII output."""
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(value[:middle])) <= budget:
            low = middle
        else:
            high = middle - 1
    return value[:low]


def path_summary(paths):
    result = []
    for path in paths[:20]:
        item = bounded_text(path, 200)
        if len(json.dumps(result + [item])) > 2000:
            break
        result.append(item)
    return result


def snapshot(workspace):
    entries, total = {}, 0
    includes = getattr(workspace, "snapshot_includes", [])

    def visit(base, name):
        p = Path(base, name)
        rel = p.relative_to(workspace.root).as_posix()
        return not p.is_relative_to(workspace.artifacts) and (
            name not in IGNORED
            or (
                name != ".git"
                and any(
                    x == "."
                    or x == rel
                    or x.startswith(rel + "/")
                    or rel.startswith(x + "/")
                    for x in includes
                )
            )
        )

    def walk_error(exc):
        raise exc

    try:
        for base, dirs, names in os.walk(
            workspace.root, followlinks=False, onerror=walk_error
        ):
            dirs[:] = sorted(d for d in dirs if visit(base, d))
            for name in sorted(names + [d for d in dirs if Path(base, d).is_symlink()]):
                p = Path(base, name)
                if p.is_relative_to(workspace.artifacts):
                    continue
                rel = p.relative_to(workspace.root).as_posix()
                st = p.lstat()
                if len(entries) >= 10000 or total + st.st_size > 256 * 1024 * 1024:
                    raise ValueError("Workspace snapshot budget exceeded")
                total += st.st_size
                if stat.S_ISLNK(st.st_mode):
                    data = os.readlink(p).encode()
                    kind = "symlink"
                elif stat.S_ISREG(st.st_mode):
                    if st.st_size > 32 * 1024 * 1024:
                        raise ValueError("File exceeds 32 MiB snapshot budget: " + rel)
                    with p.open("rb") as f:
                        data = f.read(32 * 1024 * 1024 + 1)
                    if len(data) != st.st_size or len(data) > 32 * 1024 * 1024:
                        raise ValueError("File changed during snapshot: " + rel)
                    kind = "file"
                else:
                    raise ValueError("Unsupported workspace file: " + rel)
                entries[rel] = {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                    "kind": kind,
                    "mode": stat.S_IMODE(st.st_mode),
                }
        # Include directory existence, including empty expected/preserved directories.
        for base, dirs, _ in os.walk(
            workspace.root, followlinks=False, onerror=walk_error
        ):
            dirs[:] = sorted(
                d for d in dirs if visit(base, d) and not Path(base, d).is_symlink()
            )
            for d in dirs:
                p = Path(base, d)
                if not p.is_relative_to(workspace.artifacts):
                    entries[p.relative_to(workspace.root).as_posix() + "/"] = {
                        "kind": "directory"
                    }
            if len(entries) > 10000:
                raise ValueError("Workspace snapshot budget exceeded")
        encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        return {
            "complete": True,
            "hash": hashlib.sha256(encoded).hexdigest(),
            "entries": entries,
        }
    except (OSError, ValueError) as exc:
        return {
            "complete": False,
            "hash": None,
            "entries": entries,
            "error": workspace.redact(str(exc)),
        }


def verify(workspace, spec, baseline, stop_event, deadline):
    before = snapshot(workspace)
    changes = [
        {
            "path": p,
            "before": baseline["entries"].get(p),
            "after": before["entries"].get(p),
        }
        for p in sorted(baseline["entries"].keys() | before["entries"].keys())
        if baseline["entries"].get(p) != before["entries"].get(p)
    ]
    violations = [
        c["path"]
        for c in changes
        if any(
            protected == "."
            or c["path"].rstrip("/") == protected
            or c["path"].startswith(protected.rstrip("/") + "/")
            for protected in spec.preserve_paths
        )
    ]
    outputs = []
    for expected in spec.expected_outputs:
        p = workspace.path(expected.path)
        passed = {
            "file": p.is_file(),
            "directory": p.is_dir(),
            "absent": not p.exists() and not p.is_symlink(),
        }[expected.kind]
        outputs.append({**expected.model_dump(), "passed": passed})
    checks = []
    stale = False
    for check in spec.required_checks:
        if stop_event.is_set() or time.monotonic() >= deadline:
            break
        workspace.command_timeout = min(
            check.timeout_seconds, max(0.01, deadline - time.monotonic())
        )
        started = time.time()
        task = workspace.run_command(check.command, check.cwd)
        pid = task["process_id"]
        record = workspace.processes[pid]
        while not record["done"].wait(0.1):
            if stop_event.is_set() or time.monotonic() >= deadline:
                workspace.stop_process(pid)
                break
        observed = workspace.read_process(pid)
        observed["output"] = bounded_text(observed["output"], 250)
        data = record["log"].read_bytes() if record["log"].exists() else b""
        checks.append(
            {
                **check.model_dump(),
                **observed,
                "started_at": started,
                "ended_at": time.time(),
                "snapshot_hash": before["hash"],
                "log_sha256": hashlib.sha256(data).hexdigest(),
                "log_bytes": len(data),
                "timed_out": record["timed_out"],
                "cancelled": record["cancelled"],
            }
        )
        after = snapshot(workspace)
        stale |= not after["complete"] or after["hash"] != before["hash"]
        if stale:
            break
    final = snapshot(workspace)
    stale |= not final["complete"] or final["hash"] != before["hash"]
    artifact = workspace.artifacts / "manifest.json"
    manifest = {
        "baseline": baseline,
        "before_checks": before,
        "final": final,
        "changes": changes,
    }
    encoded = json.dumps(manifest, sort_keys=True).encode()
    artifact.write_bytes(encoded)
    evidence = {
        "assignment_hash": spec.digest(),
        "snapshot_complete": baseline["complete"]
        and before["complete"]
        and final["complete"],
        "snapshot_hash": before["hash"],
        "baseline_hash": baseline["hash"],
        "final_hash": final["hash"],
        "manifest_artifact": artifact.name,
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "change_count": len(changes),
        "changed_paths": path_summary([c["path"] for c in changes]),
        "changes_truncated": len(changes)
        > len(path_summary([c["path"] for c in changes])),
        "checks": checks,
        "outputs": outputs,
        "violations": path_summary(violations),
        "stale": stale,
        "snapshot_scope": {
            "root": str(workspace.root),
            "outside_workspace_observed": False,
            "excluded_directories": sorted(IGNORED),
            "explicit_includes": getattr(workspace, "snapshot_includes", []),
        },
        "snapshot_error": baseline.get("error")
        or before.get("error")
        or final.get("error"),
    }
    evidence["status"] = verification_status(spec, evidence)
    return workspace.clean(evidence)
