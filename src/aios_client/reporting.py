"""Transport useful worker findings without another inference or raw tool traces."""

from __future__ import annotations

import hashlib
import json
import re

from .verification import bounded_text

REPORT = re.compile(r"aios-experiment-[0-9a-fA-F-]{36}\.json\Z")


def _object(text):
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("outcome") not in ("completed", "partial", "blocked")
        or not isinstance(value.get("evidence"), list)
        or not isinstance(value.get("limitations"), list)
    ):
        return None
    return value


def result_summary(workspace, spec, report, *, execution_failed=False):
    """Prefer a structured final answer; bridge existing experimental JSON reports.

    Only explicitly expected experimental reports are opened. Report files must
    match the immutable-check snapshot; their statements remain worker claims.
    Never follow artifact paths supplied by the model to collect more files.
    """
    selected = getattr(workspace, "published_result", None)
    value = dict(selected) if selected is not None else _object(report)
    source = "publish_result" if selected is not None else "final_response"
    source_hash = None
    if value is None:
        manifest = workspace.artifacts / "manifest.json"
        try:
            entries = json.loads(manifest.read_text())["final"]["entries"]
        except (OSError, ValueError, KeyError, TypeError):
            entries = {}
        for output in spec.expected_outputs:
            if output.kind != "file" or not REPORT.fullmatch(output.path):
                continue
            try:
                path = workspace.path(output.path)
                with path.open("rb") as handle:
                    data = handle.read(128 * 1024 + 1)
                if len(data) > 128 * 1024:
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if entries.get(output.path, {}).get("sha256") != digest:
                    continue
                candidate = _object(data.decode("utf-8"))
                if candidate is not None:
                    value, source, source_hash = candidate, output.path, digest
                    break
            except (OSError, ValueError, UnicodeError):
                continue
    if value is None:
        return None  # Legacy prose stays available; no fabricated outcome.

    if execution_failed:
        value = {
            **value,
            "outcome": "blocked" if value["outcome"] == "blocked" else "partial",
            "limitations": [
                "Execution did not finish; final host verification is unavailable",
                *value.get("limitations", []),
            ],
        }

    def text(value, limit):
        return (
            bounded_text(workspace.redact(value), limit)
            if isinstance(value, str)
            else ""
        )

    def items(values, count, limit):
        if not isinstance(values, list):
            return []
        result = []
        for item in values[:count]:
            if isinstance(item, (dict, list)):
                item = json.dumps(item, ensure_ascii=False)
            if isinstance(item, str):
                result.append(text(item, limit))
        if len(values) > count:
            result.append(
                f"[{len(values) - count} additional entries retained in source report]"
            )
        return result

    content = value.get("requested_content")
    content = content if isinstance(content, list) else []
    sections = []
    for section in content[:4]:
        if not isinstance(section, dict) or not isinstance(section.get("text"), str):
            continue
        original = workspace.redact(section["text"])
        body = bounded_text(original, 8000)
        item = {
            "label": text(section.get("label"), 160),
            "text": body,
            "truncated": body != original or bool(section.get("source_truncated")),
        }
        if source == "publish_result":
            item["source_kind"] = section.get("source_kind", "worker_text")
            if item["source_kind"] == "workspace_file":
                for key in ("path", "source_sha256", "start_line", "end_line"):
                    item[key] = section[key]
                try:
                    manifest = json.loads(
                        (workspace.artifacts / "manifest.json").read_text()
                    )
                    final = manifest["final"]["entries"].get(item["path"])
                    item["matches_final_snapshot"] = (
                        (final.get("sha256") == item["source_sha256"])
                        if final is not None
                        else None
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    item["matches_final_snapshot"] = None
        sections.append(item)
    result = {
        "outcome": value["outcome"],
        "summary": text(value.get("summary"), 1000),
        "findings": items(value.get("findings", value["evidence"]), 6, 700),
        "limitations": items(value["limitations"], 4, 500),
        "artifacts": items(value.get("artifacts", []), 6, 400),
        "requested_content": sections,
        "content_sections_omitted": max(0, len(content) - 4),
        "source": source,
        "source_sha256": source_hash,
    }
    # Preserve explicit omissions under the encoded transport budget, including
    # non-ASCII text. Never silently ship a prefix of invalid JSON.
    result["transport_truncated"] = False
    while len(json.dumps(result)) > 18000:
        result["transport_truncated"] = True
        if result["requested_content"]:
            section = result["requested_content"][-1]
            if len(section["text"]) > 256:
                section["text"] = section["text"][: len(section["text"]) // 2]
                section["truncated"] = True
            else:
                result["requested_content"].pop()
                result["content_sections_omitted"] += 1
        elif result["findings"]:
            result["findings"].pop()
        else:
            break
    return result
