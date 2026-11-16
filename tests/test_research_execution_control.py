"""Regression tests for Calibre research loops observed in the 2026-11-16 run."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx

from aios_client.research.control import ResearchControl
from aios_client.research.evidence import EvidenceStore
from aios_client.research.sources import ResearchSource
from aios_client.research.tools import ResearchTools


ATOM = "http://www.w3.org/2005/Atom"
FEED = f"""<feed xmlns="{ATOM}"><entry>
<title>Glacial Geology of Michigan</title><id>urn:uuid:one</id>
<author><name>Scientist</name></author><summary>Glacial evidence in Michigan</summary>
<link rel="http://opds-spec.org/acquisition" type="application/pdf"
 href="/book.pdf"/></entry></feed>"""


class Host:
    def guard(self):
        pass

    def redact(self, value):
        return value


def source():
    return ResearchSource(id="calibre", adapter="calibre_opds",
                          endpoint="http://192.168.1.16/opds")


def test_search_dedup_and_calibre_acquisition_cache(monkeypatch):
    requests = []
    def serve(request):
        requests.append(str(request.url))
        if "/search/" in request.url.path:
            return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                                  text=FEED)
        return httpx.Response(200, headers={"content-type": "application/pdf"},
                              content=b"sample-pdf")
    tool = ResearchTools(Host(), [source()], httpx.MockTransport(serve))
    monkeypatch.setattr("aios_client.research.tools.extract_document",
                        lambda data, ext: ([("page:1", "Ice sheet evidence"),
                                            ("page:2", "Moraines")], []))
    first = tool.research_search("glacier", "calibre")
    assert first["status"] == "ok"
    assert first["search_mode"] == "full_text"
    repeated = tool.research_search("  GLACIER  ", "calibre", limit=10)
    assert repeated["cache_hit"] is True
    assert tool.control.searches == 1
    evidence_id = first["results"][0]["evidence_id"]
    one = tool.research_read(evidence_id, section_index=0)
    two = tool.research_read(evidence_id, section_index=1)
    three = tool.research_read(evidence_id, section_index=0)
    assert one["text"] == "Ice sheet evidence"
    assert two["section"] == "page:2"
    assert three["cache_hit"] is True
    assert tool.control.acquisitions == 1
    assert tool.control.passages == 2
    assert sum("/book.pdf" in url for url in requests) == 1


def test_search_http_failure_is_not_an_empty_catalog():
    tool = ResearchTools(Host(), [source()], httpx.MockTransport(
        lambda request: httpx.Response(403)))
    result = tool.research_search("glacier", "calibre")
    assert result["status"] == "source_error"
    assert result["errors"] == {"calibre": "http_status"}
    assert tool.research_search("GLACIER", "calibre")["cache_hit"] is True


def test_research_limits_block_new_queries_and_reads():
    control = ResearchControl(max_searches=1, max_acquisitions=1,
                              max_passages=1, max_failed_acquisitions=1)
    assert control.allowed("search")
    control.searches += 1
    assert not control.allowed("search")
    assert control.allowed("acquisition")
    control.failed_acquisitions += 1
    assert not control.allowed("acquisition")
    assert control.allowed("passage")
    control.passages += 1
    assert not control.allowed("passage")
    assert control.snapshot()["blocked_operations"] == 3


def test_receipt_preserves_all_sections_and_search_does_not_downgrade(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.sqlite3", "test-run")
    item = {"evidence_id": "a" * 24, "source_id": "calibre",
            "kind": "calibre_book", "title": "A book",
            "location": {"catalog_id": "urn:uuid:a"}, "excerpt": ""}
    store.save(item)
    store.save(item, verified=True, section="page:2", document_sha256="x" * 64)
    store.save(item, verified=True, section="page:5", document_sha256="x" * 64)
    store.save(item)
    receipt = store.get(item["evidence_id"])
    assert receipt["read_verified"] is True
    assert receipt["sections"] == ["page:2", "page:5"]


def test_research_fallback_reports_evidence_without_inventing_findings():
    from aios_client.agent.session import AgentSession
    control = ResearchControl()
    control.verified_passages.append({
        "evidence_id": "a" * 24, "section": "page:4", "document_sha256": "x" * 64,
    })
    tools = SimpleNamespace(control=control, evidence={
        "a" * 24: {"public": {"source_id": "calibre", "title": "An observed source"}}
    })
    events = []
    session = object.__new__(AgentSession)
    session.workspace = SimpleNamespace(research_tools=tools)
    session.record = lambda kind, body: events.append((kind, body))
    report = json.loads(session.research_fallback("Step budget exhausted"))
    assert report["outcome"] == "partial"
    assert report["evidence"][0]["title"] == "An observed source"
    assert "geological" not in report["summary"].lower()
    assert "Step budget exhausted" in report["limitations"]
    assert events[0][0] == "research_partial_report"


def test_fallback_blocks_when_no_source_text_was_retrieved():
    from aios_client.agent.session import AgentSession
    session = object.__new__(AgentSession)
    session.workspace = SimpleNamespace(
        research_tools=SimpleNamespace(control=ResearchControl(), evidence={}))
    session.record = lambda *args: None
    report = json.loads(session.research_fallback("Research budget exhausted"))
    assert report["outcome"] == "blocked"
    assert report["evidence"] == []
