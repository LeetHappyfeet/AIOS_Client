"""Incomplete Calibre work retains evidence for read_work_result, not just an empty findings list."""
from __future__ import annotations

import json
from types import SimpleNamespace

from aios_client.reporting import result_summary


def workspace(payload):
    return SimpleNamespace(
        registry=SimpleNamespace(tool_mode="research"),
        published_result=None,
        redact=lambda x: x,
    )


def report():
    return {
        "objective": "Use calibre only",
        "outcome": "partial",
        "summary": "Five retrieved passages; synthesis truncated",
        "evidence": [
            {"evidence_id": "a" * 24, "source_id": "calibre",
             "title": "Quaternary", "section": "page:7",
             "note": "Passage retrieved; no independent claim verified"}
        ],
        "findings": [],
        "limitations": ["Model synthesis truncated"],
        "search_log": [
            {"query": "striations", "source_id": "calibre",
             "status": "source_error", "errors": {"calibre": "http_status_404"},
             "attempted_sources": ["calibre"], "result_count": 0}
        ],
        "research_progress": {
            "searches": 1, "acquisitions": 1, "passages": 1, "failed_acquisitions": 0,
            "verified_passages": [{
                "evidence_id": "a" * 24, "source_id": "calibre",
                "title": "Quaternary", "section": "page:7",
                "document_sha256": "b" * 64,
                "passage_excerpt": "The central North American ice sheet expanded.",
                "url": "http://private.example/should-not-escape",
            }],
        },
    }


def test_partial_report_preserves_readable_passage_and_error_status():
    source = report()
    spec = SimpleNamespace()
    w = workspace(source)
    summary = result_summary(w, spec, json.dumps(source))
    assert summary["findings"] == []
    assert summary["evidence"][0]["section"] == "page:7"
    assert "central North American" in summary["research_progress"]["verified_passages"][0]["passage_excerpt"]
    assert summary["search_log"][0]["errors"]["calibre"] == "http_status_404"
    assert "private.example" not in json.dumps(summary)
    assert len(json.dumps(summary)) < 18000


def test_large_report_retains_bounded_evidence_without_exposing_urls():
    source = report()
    source["research_progress"]["verified_passages"] = [
        {"evidence_id": format(i, "024x"), "source_id": "calibre",
         "title": "A" * 500, "section": f"page:{i}",
         "passage_excerpt": "x" * 5000, "secret": "do not include"}
        for i in range(40)
    ]
    source["search_log"] = [
        {"query": "q" * 1000, "source_id": "calibre", "status": "source_error",
         "errors": {"calibre": "http_status_404"}}
        for _ in range(30)
    ]
    value = result_summary(workspace(source), SimpleNamespace(), json.dumps(source))
    assert len(json.dumps(value)) <= 18000
    assert len(value["research_progress"]["verified_passages"]) <= 8
    assert len(value["search_log"]) <= 12
    assert "do not include" not in json.dumps(value)
