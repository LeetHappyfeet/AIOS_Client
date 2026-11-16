"""Small, evidence-grounded research synthesis: the host owns the audit ledger.

Never turns a search failure into an absence claim, nor a passage receipt into
verified scientific conclusions. The model owns interpretations, bounded by
retained citation receipts; the host inserts deterministic search metadata.
"""
from __future__ import annotations

import json


def _verified_passages(tools, limit=8):
    unique = {}
    for item in tools.control.verified_passages:
        key = (item.get("evidence_id"), item.get("section"))
        if key[0] and key[1]:
            unique[key] = item
    return list(unique.values())[-limit:]


def compact_synthesis_messages(spec, tools):
    """Reporting-only context: no giant assignment, no raw chat/tool history."""
    passages = []
    for row in _verified_passages(tools):
        passages.append({
            "evidence_id": row.get("evidence_id"),
            "source_id": row.get("source_id"),
            "title": row.get("title"),
            "section": row.get("section"),
            "passage_excerpt": str(row.get("passage_excerpt") or "")[:420],
        })
    audit = list(getattr(tools, "search_audit", []))[-12:]
    brief = {
        "assignment": str(getattr(spec, "objective", "Research assignment"))[:1200],
        "passages": passages,
        "search_statuses": audit,
        "search_count": tools.control.searches,
        "attempted_acquisitions": tools.control.acquisitions,
        "unread_candidates": [
            {"title": x.get("title"), "source_id": x.get("source_id")}
            for x in tools.control.candidates[-8:] if x.get("readable")
        ][:5],
    }
    instruction = (
        "Return ONLY a compact JSON object with: outcome (completed,partial,blocked),"
        " summary (at most 300 characters), findings (at most 3 objects with"
        " observation, interpretation, evidence_id, source_id, section),"
        " evidence (brief list), and limitations (at most 5 short strings)."
        " Cite only provided passage IDs and sections; do not quote whole pages."
        " Treat search errors, unread books, and omitted searches as UNKNOWN COVERAGE,"
        " never evidence that the library lacks the subject."
        " The host will attach the authoritative objective, exact search-status log,"
        " research metrics and required output file. Do NOT repeat them."
        " Keep response under 1300 tokens, even when evidence is insufficient."
    )
    return [
        {"role": "system", "content": instruction},
        {"role": "user", "content": json.dumps(brief, ensure_ascii=False)},
    ]


def merge_research_report(spec, tools, report):
    """Append authoritative audit without laundering model-generated findings."""
    data = json.loads(report)
    if not isinstance(data, dict):
        raise ValueError("Research synthesis must be a JSON object")
    if data.get("outcome") not in {"completed", "partial", "blocked"}:
        raise ValueError("Invalid research outcome")
    if not isinstance(data.get("evidence"), list) or not isinstance(
        data.get("limitations"), list
    ):
        raise ValueError("Missing research evidence or limitations")
    data["objective"] = str(getattr(spec, "objective", "Research assignment"))
    data["search_log"] = list(getattr(tools, "search_audit", []))
    data["research_progress"] = tools.control.snapshot()
    data.setdefault("findings", [])
    data.setdefault("artifacts", [])
    data.setdefault("requested_content", [])
    if not isinstance(data["findings"], list):
        data["findings"] = []
    # A failed search or unread acquisition cannot establish whole-library absence.
    unresolved = any(row.get("status") in {"source_error", "source_unhealthy"}
                     for row in data["search_log"])
    if unresolved:
        caveat = ("Some Calibre searches failed or were not attempted; "
                  "library-wide absence of relevant material is not established.")
        if caveat not in data["limitations"]:
            data["limitations"].append(caveat)
        if data["outcome"] == "completed":
            data["outcome"] = "partial"
    return json.dumps(data, ensure_ascii=False)
