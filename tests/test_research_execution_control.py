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
    secrets = ()
    command_timeout = 120
    assignment_deadline = None

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
                        lambda data, ext, **kwargs: ([("page:1", "Ice sheet evidence"),
                                            ("page:2", "Moraines")], []))
    first = tool.research_search("glacier", "calibre")
    assert first["status"] == "ok"
    assert first["search_mode"] == "full_text"
    assert first["results"][0]["readable"] is True
    assert first["results"][0]["author"] == "Scientist"
    assert "application/pdf" in first["results"][0]["available_formats"]
    repeated = tool.research_search("  GLACIER  ", "calibre", limit=10)
    assert repeated["cache_hit"] is True
    assert tool.control.searches == 1
    assert tool.control.queries[0]["query"] == "glacier"
    assert tool.control.candidates[0]["status"] == "discovery_only"
    evidence_id = first["results"][0]["evidence_id"]
    one = tool.research_read(evidence_id, section_index=0)
    two = tool.research_read(evidence_id, section_index=1)
    three = tool.research_read(evidence_id, section_index=0)
    assert one["text"] == "Ice sheet evidence"
    assert two["section"] == "page:2"
    assert one["download_cache_hit"] is False
    assert two["download_cache_hit"] is True
    assert three["cache_hit"] is True
    assert tool.control.acquisitions == 1
    assert tool.control.passages == 2
    ledger = tool.control.snapshot()["verified_passages"]
    assert ledger[0]["title"] == "Glacial Geology of Michigan"
    assert ledger[0]["passage_excerpt"] == "Ice sheet evidence"
    assert ledger[1]["section"] == "page:2"
    assert sum("/book.pdf" in url for url in requests) == 1


def test_search_http_failure_is_not_an_empty_catalog():
    tool = ResearchTools(Host(), [source()], httpx.MockTransport(
        lambda request: httpx.Response(403)))
    result = tool.research_search("glacier", "calibre")
    assert result["status"] == "source_error"
    assert result["errors"] == {"calibre": "http_status_403"}
    assert result["error_details"]["calibre"]["full_text_error"] == "http_status_403"
    assert result["error_details"]["calibre"]["final_search_error"] == "http_status_403"
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
    assert report["findings"] == []
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


def test_oversized_book_fails_once_without_repeated_download(monkeypatch):
    requests = []
    def serve(request):
        requests.append(str(request.url))
        if "/search/" in request.url.path:
            return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                                  text=FEED)
        return httpx.Response(200, headers={"content-type": "application/pdf"},
                              content=b"x" * (1024 * 1024 + 1))
    bounded_source = source().model_copy(update={"max_document_bytes": 1024 * 1024})
    tool = ResearchTools(Host(), [bounded_source], httpx.MockTransport(serve))
    result = tool.research_search("glacier", "calibre")
    eid = result["results"][0]["evidence_id"]
    first = tool.research_read(eid, section_index=0)
    second = tool.research_read(eid, section_index=1)
    assert first["error"] == second["error"] == "response_too_large"
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert first["limit_bytes"] == 1024 * 1024
    assert tool.control.failed_acquisitions == 1
    assert tool.control.passages == 1  # cached failure does not consume passages
    assert sum("/book.pdf" in url for url in requests) == 1


def test_research_session_reserves_final_steps_and_caps_model_calls():
    """A model that refuses to summarize cannot consume a 60-step research run."""
    import threading
    import time
    from aios_client.agent.session import AgentSession

    calls = []
    class Model:
        http = SimpleNamespace(timeout=SimpleNamespace(read=5))
        def completion(self, model_id, messages, tools, timeout=None):
            calls.append({"tools": tools})
            return {"content": "not json", "tool_calls": []}

    class Context:
        state = {}
        history = []
        compacted = 0
        def prepare(self):
            return [{"role": "system", "content": "research"}]
        def append(self, message):
            pass

    control = ResearchControl()
    research_tools = SimpleNamespace(control=control, evidence={}, sources={"calibre": object()})
    registry = SimpleNamespace(tool_mode="research", catalog=lambda: [
        {"type": "function", "function": {"name": "research_search"}}])
    workspace = SimpleNamespace(
        research_tools=research_tools, registry=registry,
        process_status=lambda: {"processes": []}, host_info=lambda: {},
        guard=lambda: None, clean=lambda value: value, redact=lambda value: value,
        published_result=None, model_diagnostics={},
    )
    spec = SimpleNamespace(budgets=SimpleNamespace(model_steps=60),
                           digest=lambda: "digest")
    session = object.__new__(AgentSession)
    session.workspace = workspace
    session.registry = registry
    session.spec = spec
    session.tasks = SimpleNamespace(plan=[], explanation="", blocked=None)
    session.model = Model()
    session.model_id = "test-model"
    session.context = Context()
    session.hud_chars = 0
    session.instructions = SimpleNamespace(loaded={})
    session.deadline = time.monotonic() + 60
    session.gate = threading.Lock()
    session.control = lambda: None
    session.record = lambda *args: None
    session.checkpoint = lambda: None
    report = json.loads(session.run())
    assert len(calls) == 20
    assert all(not call["tools"] for call in calls[-3:])
    assert report["outcome"] == "blocked"
    assert "report" not in report.get("summary", "").lower() or report["findings"] == []


def test_reporting_phase_rejects_more_tool_calls():
    import threading
    import time
    from aios_client.agent.session import AgentSession

    attempts = []
    class Model:
        http = SimpleNamespace(timeout=SimpleNamespace(read=5))
        def completion(self, model_id, messages, tools, timeout=None):
            attempts.append(tools)
            return {"content": "", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "research_search", "arguments": '{"query":"glacier"}'}
            }]}

    session = object.__new__(AgentSession)
    research_tools = SimpleNamespace(control=ResearchControl(), evidence={},
                                     sources={"calibre": object()})
    session.workspace = SimpleNamespace(
        research_tools=research_tools,
        process_status=lambda: {"processes": []}, host_info=lambda: {},
        guard=lambda: None, clean=lambda x: x, redact=lambda x: x,
        published_result=None, model_diagnostics={})
    session.registry = SimpleNamespace(tool_mode="research",
                                       catalog=lambda: ["research_search"])
    session.spec = SimpleNamespace(budgets=SimpleNamespace(model_steps=1),
                                   digest=lambda: "digest")
    session.tasks = SimpleNamespace(plan=[], explanation="", blocked=None)
    session.model = Model()
    session.model_id = "test-model"
    session.context = SimpleNamespace(state={}, history=[], compacted=0,
                                      prepare=lambda: [], append=lambda msg: None)
    session.instructions = SimpleNamespace(loaded={})
    session.hud_chars = 0
    session.gate = threading.Lock()
    session.deadline = time.monotonic() + 60
    session.control = lambda: None
    session.record = lambda *args: None
    session.checkpoint = lambda: None
    result = json.loads(session.run())
    assert len(attempts) == 1
    assert attempts[0] == []
    assert result["outcome"] == "blocked"
    assert research_tools.control.searches == 0


def test_multiterm_calibre_metadata_fallback_uses_quoted_expression():
    observed = []
    def serve(request):
        observed.append(str(request.url))
        if request.url.params.get("full_text") == "y":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                              text=FEED)
    tools = ResearchTools(Host(), [source()], httpx.MockTransport(serve))
    response = tools.research_search("laurentide ice sheet", "calibre")
    assert response["status"] == "ok"
    assert response["search_mode"] == "metadata"
    assert response["full_text_error"] == "http_status_404"
    assert len(observed) == 2
    assert "laurentide%20ice%20sheet" in observed[0]
    assert "%22laurentide%20ice%20sheet%22" in observed[1]


def test_recommended_candidate_takes_priority_over_next_search():
    def serve(request):
        return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                              text=FEED)
    tool = ResearchTools(Host(), [source()], httpx.MockTransport(serve))
    first = tool.research_search("glacial", "calibre")
    assert first["status"] == "ok"
    # Two unique searches make further discovery wait for existing evidence.
    assert tool.research_search("moraine", "calibre")["status"] == "ok"
    third = tool.research_search("varves", "calibre")
    assert third["status"] == "read_recommended"
    assert third["recommended_evidence"]["title"] == "Glacial Geology of Michigan"
    assert third["recommended_evidence"]["author"] == "Scientist"
    assert tool.control.searches == 2
    assert third["recommended_evidence"]["content_scope"] == "search_excerpt_only"


def test_unhealthy_calibre_search_preserves_single_word_probe_budget():
    def serve(request):
        return httpx.Response(404)
    tool = ResearchTools(Host(), [source()], httpx.MockTransport(serve))
    for keyword in ("ice sheet", "glacial drift", "proglacial lakes"):
        assert tool.research_search(keyword, "calibre")["status"] == "source_error"
    assert tool.control.searches == 3
    response = tool.research_search("great lakes geology", "calibre")
    assert response["status"] == "source_unhealthy"
    assert tool.control.searches == 3
    one_word = tool.research_search("glacier", "calibre")
    assert one_word["status"] == "source_error"
    assert tool.control.searches == 4


def test_unread_candidate_can_be_opened_after_search_quota_but_not_report_reserve():
    import threading
    import time
    from aios_client.agent.session import AgentSession

    calls = []
    class Model:
        http = SimpleNamespace(timeout=SimpleNamespace(read=5))
        def completion(self, model_id, messages, tools, timeout=None):
            calls.append(tools)
            return {"content": "not json", "tool_calls": []}

    class Context:
        state = {}
        history = []
        compacted = 0
        def prepare(self):
            return []
        def append(self, message):
            pass

    control = ResearchControl()
    control.searches = control.max_searches
    tools = SimpleNamespace(
        control=control, evidence={}, sources={"calibre": object()},
        source_failures={},
        recommended_unread=lambda: {
            "evidence_id": "a" * 24, "title": "Glacial Processes and Sediments"
        },
    )
    session = object.__new__(AgentSession)
    session.workspace = SimpleNamespace(
        research_tools=tools,
        process_status=lambda: {"processes": []}, host_info=lambda: {},
        guard=lambda: None, clean=lambda x: x, redact=lambda x: x,
        published_result=None, model_diagnostics={},
    )
    session.registry = SimpleNamespace(
        tool_mode="research", catalog=lambda: [
            {"function": {"name": "research_read"}},
            {"function": {"name": "research_search"}},
        ]
    )
    session.spec = SimpleNamespace(
        objective="Find glaciation evidence",
        budgets=SimpleNamespace(model_steps=4), digest=lambda: "digest"
    )
    session.tasks = SimpleNamespace(plan=[], explanation="", blocked=None)
    session.model = Model()
    session.model_id = "test"
    session.context = Context()
    session.hud_chars = 0
    session.instructions = SimpleNamespace(loaded={})
    session.deadline = time.monotonic() + 60
    session.gate = threading.Lock()
    session.control = lambda: None
    session.record = lambda *args: None
    session.checkpoint = lambda: None
    report = json.loads(session.run())
    assert len(calls) == 4
    assert all(calls[i] for i in (0, 1, 2))
    assert all(call[0]["function"]["name"] == "research_read" for call in calls[:3])
    assert calls[-1] == []
    assert session.context.state["recommended_unread_evidence"]["evidence_id"] == "a" * 24
    assert report["outcome"] == "blocked"


def test_first_read_chooses_relevant_pdf_page_for_compound_query(monkeypatch):
    def serve(request):
        if "/search/" in request.url.path:
            return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                                  text=FEED)
        return httpx.Response(200, headers={"content-type": "application/pdf"},
                              content=b"mock-pdf")
    monkeypatch.setattr("aios_client.research.tools.extract_document",
                        lambda data, kind, **kwargs: ([
                            ("page:1", "General science and basic chemistry"),
                            ("page:2", "Pleistocene glacial deposits around Great Lakes moraines"),
                            ("page:3", "A non-glacial chapter on rock types"),
                        ], []))
    tool = ResearchTools(Host(), [source()], httpx.MockTransport(serve))
    result = tool.research_search("pleistocene glacier great lakes", "calibre")
    eid = result["results"][0]["evidence_id"]
    passage = tool.research_read(eid)
    assert passage["section"] == "page:2"
    assert "Pleistocene" in passage["text"]
    assert passage["passage_retrieved"] is True


def test_candidate_after_sixth_position_survives_research_context_ledger():
    """A later on-topic book must not vanish behind generic first-page hits."""
    atom = "http://www.w3.org/2005/Atom"
    rows = []
    for i in range(7):
        title = "Glacial Processes and Sediments" if i == 6 else "Daily News"
        rows.append(
            f'<entry><title>{title}</title><id>urn:uuid:{i}</id>'
            f'<author><name>Author</name></author>'
            f'<link rel="http://opds-spec.org/acquisition" '
            f'type="application/pdf" href="/book{i}.pdf"/></entry>'
        )
    feed = '<feed xmlns="' + atom + '">' + "".join(rows) + "</feed>"
    tool = ResearchTools(
        Host(), [source()], httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/atom+xml"}, text=feed
            )
        ),
    )
    result = tool.research_search("glacial", "calibre", limit=10)
    expected_id = next(x["evidence_id"] for x in result["results"]
                       if x["title"] == "Glacial Processes and Sediments")
    assert any(item["evidence_id"] == expected_id
               for item in tool.control.snapshot()["candidates"])
    assert tool.recommended_unread()["evidence_id"] == expected_id


def test_calibre_404_diagnoses_root_and_recovers_via_broad_metadata():
    requests = []
    def serve(request):
        requests.append(str(request.url))
        if request.url.path == "/opds":
            return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                                  text=f'<feed xmlns="{ATOM}"><link rel="search" href="/opds/search/{{searchTerms}}"/></feed>')
        if "all%3A" in str(request.url):
            return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                                  text=FEED)
        return httpx.Response(404)
    tool = ResearchTools(Host(), [source()], httpx.MockTransport(serve))
    response = tool.research_search("laurentide ice sheet", "calibre")
    assert response["status"] == "ok"
    assert response["search_mode"] == "metadata_broad"
    assert response["full_text_error"] == "http_status_404"
    assert len(requests) == 4
    assert sum(url.endswith("/opds") for url in requests) == 1
    assert tool.calibre_search_probes["calibre"]["advertised_search"] is True


def test_calibre_all_query_failure_preserves_exact_http_error():
    def serve(request):
        if request.url.path == "/opds":
            return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                                  text=f'<feed xmlns="{ATOM}"></feed>')
        return httpx.Response(404)
    tool = ResearchTools(Host(), [source()], httpx.MockTransport(serve))
    response = tool.research_search("laurentide ice sheet", "calibre")
    assert response["status"] == "source_error"
    assert response["errors"]["calibre"] == "http_status_404"
    assert tool.calibre_search_probes["calibre"]["advertised_search"] is False
    assert response["error_details"]["calibre"]["final_search_error"] == "http_status_404"


def test_calibre_configurable_larger_document_download(monkeypatch):
    # A 7 MiB PDF exceeds the previous global 6 MiB bound but fits the
    # per-source default (12 MiB), without relaxing extracted-text limits.
    seen_limits = []
    def serve(request):
        if "/search/" in request.url.path:
            return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                                  text=FEED)
        return httpx.Response(200, headers={"content-type": "application/pdf"},
                              content=b"x" * (7 * 1024 * 1024))
    def extraction(data, ext, *, max_bytes):
        seen_limits.append((len(data), max_bytes))
        return [("page:1", "Ice flow observations")], []
    monkeypatch.setattr("aios_client.research.tools.extract_document", extraction)
    tool = ResearchTools(Host(), [source()], httpx.MockTransport(serve))
    search = tool.research_search("glacier", "calibre")
    result = tool.research_read(search["results"][0]["evidence_id"])
    assert result["text"] == "Ice flow observations"
    assert seen_limits == [(7 * 1024 * 1024, 12 * 1024 * 1024)]
