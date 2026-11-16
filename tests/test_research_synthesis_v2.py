"""Regression tests for compact, bounded research synthesis and search provenance."""
from __future__ import annotations

import json
from types import SimpleNamespace

from aios_client.research.synthesis import compact_synthesis_messages, merge_research_report
from aios_client.research.control import ResearchControl


def worker():
    control = ResearchControl()
    control.searches = 3
    control.acquisitions = 1
    control.verified_passages = [
        {"evidence_id": "a" * 24, "source_id": "calibre",
         "title": "Quaternary", "section": "page:2",
         "passage_excerpt": "Ice sheets expanded into temperate latitudes"},
        {"evidence_id": "a" * 24, "source_id": "calibre",
         "title": "Quaternary", "section": "page:2",
         "passage_excerpt": "Ice sheets expanded into temperate latitudes"},
    ]
    control.candidates = [{"title": "Quaternary", "source_id": "calibre",
                           "readable": True}]
    tools = SimpleNamespace(
        control=control,
        search_audit=[{"query": "laurentide ice sheet", "source_id": "calibre",
                       "status": "source_error", "errors": {"calibre": "http_status_404"},
                       "attempted_sources": ["calibre"], "result_count": 0}],
    )
    spec = SimpleNamespace(objective="Research Great Lakes glaciation using calibre ONLY")
    return tools, spec


def test_synthesis_prompt_is_small_and_uses_only_ledger():
    tools, spec = worker()
    messages = compact_synthesis_messages(spec, tools)
    assert len(messages) == 2
    assert len(json.dumps(messages)) < 5000
    assert "http_status_404" in json.dumps(messages)
    assert json.dumps(messages).count("Ice sheets expanded") == 1
    assert "UNKNOWN COVERAGE" in messages[0]["content"]
    assert "a" * 24 in messages[1]["content"]


def test_host_merges_search_log_and_rejects_unsupported_citations():
    tools, spec = worker()
    model = {
        "objective": "substitute model objective",
        "outcome": "completed",
        "summary": "No relevant books exist",
        "findings": [
            {"observation": "A passage said this", "interpretation": "Ice moved",
             "evidence_id": "a" * 24, "source_id": "calibre", "section": "page:2"},
            {"observation": "Invented observation", "interpretation": "Unknown",
             "evidence_id": "b" * 24, "source_id": "calibre", "section": "page:3"},
        ],
        "evidence": [], "limitations": [],
    }
    result = json.loads(merge_research_report(spec, tools, json.dumps(model)))
    assert result["objective"] == spec.objective
    assert result["outcome"] == "partial"
    assert len(result["findings"]) == 1
    assert result["findings"][0]["evidence_id"] == "a" * 24
    assert result["search_log"][0]["status"] == "source_error"
    assert any("not established" in x for x in result["limitations"])
    assert any("omitted" in x for x in result["limitations"])


def test_invalid_model_report_cannot_bypass_schema():
    tools, spec = worker()
    for bad in ["not json", "[]", '{"outcome":"partial"}',
                '{"outcome":"finished","evidence":[],"limitations":[]}']:
        try:
            merge_research_report(spec, tools, bad)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid research synthesis was accepted")
