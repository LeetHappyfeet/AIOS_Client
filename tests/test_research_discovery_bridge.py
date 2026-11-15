"""Source nominations require real run-scoped read receipts and cited findings."""
from types import SimpleNamespace

import pytest

from aios_client.tools.results import ResultTools
from aios_client.reporting import result_summary


EVIDENCE = "a" * 24


class FakeResearch:
    def research_evidence(self, evidence_id):
        if evidence_id != EVIDENCE:
            raise ValueError("Unknown receipt")
        return {"receipt": {
            "evidence_id": EVIDENCE, "read_verified": True, "source_id": "library",
            "kind": "calibre_book", "title": "Reference book",
            "document_sha256": "b" * 64, "section": "chapter-1",
            "location": {"catalog_id": "private-book"}, "retrieved_at": "2026-11-15T12:00:00Z",
        }}


class FakeWorkspace:
    def __init__(self, mode="research"):
        self.registry = SimpleNamespace(tool_mode=mode)
        self.research_tools = FakeResearch()
        self.published_result = None

    def guard(self):
        pass

    def redact(self, value):
        return value

    def clean(self, value):
        return value


def test_nominate_cited_retrieved_source():
    ws = FakeWorkspace()
    tool = ResultTools(ws)
    receipt = tool.publish_result(
        outcome="completed", summary="Read a reference",
        findings=["An attributed observation"],
        research_citations=[{"finding_index": 0, "evidence_id": EVIDENCE}],
        nominate_sources=[EVIDENCE, EVIDENCE],
    )
    assert receipt["source_nominations"] == 1
    nomination = ws.published_result["source_nominations"][0]
    assert nomination["public_url"] is None
    assert nomination["document_sha256"] == "b" * 64
    spec = SimpleNamespace(expected_outputs=[])
    summary = result_summary(ws, spec, "")
    assert len(summary["source_nominations"]) == 1
    assert summary["source"] == "publish_result"


def test_nomination_requires_citation_and_research_mode():
    for mode, citations in (("research", []), ("workspace", [
        {"finding_index": 0, "evidence_id": EVIDENCE}
    ])):
        ws = FakeWorkspace(mode)
        with pytest.raises(ValueError):
            ResultTools(ws).publish_result(
                outcome="completed", summary="Read a reference",
                findings=["Observation"], research_citations=citations,
                nominate_sources=[EVIDENCE],
            )
        assert ws.published_result is None


def test_unknown_evidence_cannot_be_nominated():
    ws = FakeWorkspace()
    with pytest.raises(ValueError):
        ResultTools(ws).publish_result(
            outcome="completed", summary="Read a reference",
            findings=["Observation"],
            research_citations=[{"finding_index": 0, "evidence_id": EVIDENCE}],
            nominate_sources=["c" * 24],
        )
