"""Web reads must retain source content and failures through report delivery."""
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from aios_client.reporting import result_summary
from aios_client.research.sources import ResearchSource
from aios_client.research.synthesis import compact_synthesis_messages, merge_research_report
from aios_client.research.tools import ResearchTools


class Host:
    secrets = ()
    command_timeout = 120
    assignment_deadline = None
    registry = SimpleNamespace(tool_mode="research")
    published_result = None

    def guard(self):
        pass

    def redact(self, value):
        return value


def reader(responder, *, max_bytes=250000, origins=("https://article.test",)):
    requests = []

    def serve(request):
        requests.append(str(request.url))
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{
                "title": "Survey", "content": "Snippet", "url": "https://article.test/report",
            }]})
        return responder(request)

    source = ResearchSource(id="web", adapter="searxng", endpoint="http://search.test",
                            read_origins=list(origins), max_web_bytes=max_bytes)
    host = Host()
    tools = ResearchTools(host, [source], httpx.MockTransport(serve))
    host.research_tools = tools
    eid = tools.research_search("measurement", "web")["results"][0]["evidence_id"]
    return tools, host, eid, requests


def test_two_page_ranges_survive_synthesis_and_transport_with_one_download():
    article = "First measurement is 17 m. " + "Other text. " * 450 + "Second measurement is 29 m."
    tools, host, eid, requests = reader(lambda _: httpx.Response(
        200, headers={"content-type": "text/html"}, text="<article>" + article + "</article>"))
    first = tools.research_read(eid, limit=1000)
    second = tools.research_read(eid, cursor=5300, limit=1000)
    assert first["section"] != second["section"]
    assert first["document_sha256"] == second["document_sha256"]
    assert second["download_cache_hit"] is True
    assert len(requests) == 2  # search plus one article request
    spec = SimpleNamespace(objective="Compare measurements")
    brief = json.loads(compact_synthesis_messages(spec, tools)[1]["content"])
    assert len(brief["passages"]) == 2
    assert "First measurement" in json.dumps(brief["passages"])
    assert "Second measurement" in json.dumps(brief["passages"])
    model = {"outcome": "completed", "summary": "Two measurements retrieved.",
             "findings": [{"observation": "Measured values differ.", "interpretation": "Two observations.",
                           "evidence_id": eid, "source_id": "web", "section": first["section"]}],
             "evidence": [], "limitations": []}
    report = merge_research_report(spec, tools, json.dumps(model))
    summary = result_summary(host, spec, report)
    assert len(summary["findings"]) == 1
    receipts = summary["research_progress"]["verified_passages"]
    assert len(receipts) == 2
    assert all("web_coverage" in row and "end_cursor" in row for row in receipts)
    assert tools.research_evidence(eid)["receipt"]["read_verified"] is True


@pytest.mark.parametrize("status,media,body,origins,error", [
    (200, "text/html", b"x" * 250001, ["https://article.test"], "response_too_large"),
    (403, "text/html", b"", ["https://article.test"], "http_status_403"),
    (200, "application/octet-stream", b"abc", ["https://article.test"], "unsupported_media_type"),
    (200, "text/html", b"<script>hidden</script>", ["https://article.test"], "no_text"),
    (200, "text/html", b"Text", [], "read_origin_not_approved"),
])
def test_web_read_errors_remain_visible_and_are_cached(status, media, body, origins, error):
    tools, host, eid, requests = reader(lambda _: httpx.Response(
        status, headers={"content-type": media}, content=body), origins=origins)
    read = tools.research_read(eid)
    assert read["error"] == error
    retried = tools.research_read(eid, cursor=100)
    assert retried["error"] == error and retried["cache_hit"] is True
    assert tools.control.passages == 1
    assert len(requests) == (1 if not origins else 2)
    assert tools.research_evidence(eid)["receipt"]["read_verified"] is False
    spec = SimpleNamespace(objective="Read survey")
    brief = json.loads(compact_synthesis_messages(spec, tools)[1]["content"])
    assert brief["acquisition_errors"][0]["error"] == error
    report = merge_research_report(spec, tools, json.dumps({
        "outcome": "completed", "summary": "Attempted research", "findings": [],
        "evidence": [], "limitations": [],
    }))
    summary = result_summary(host, spec, report)
    assert summary["outcome"] == "partial"
    assert summary["research_progress"]["acquisition_errors"][0]["error"] == error


def test_web_redirects_validate_each_destination():
    def serve(request):
        if request.url.path == "/report":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="measurement 12 m")

    tools, _, eid, requests = reader(serve)
    assert tools.research_read(eid)["text"] == "measurement 12 m"
    assert len(requests) == 3
    tools, _, eid, requests = reader(lambda _: httpx.Response(
        302, headers={"location": "https://unapproved.test/private"}))
    assert tools.research_read(eid)["error"] == "redirect_origin_rejected"
    assert len(requests) == 2
    tools, host, eid, requests = reader(lambda _: httpx.Response(
        302, headers={"location": "https://article.test/report?key=private-test-value"}))
    host.secrets = ("private-test-value",)
    assert tools.research_read(eid)["error"] == "redirect_origin_rejected"
    assert len(requests) == 2
    tools, _, eid, requests = reader(lambda _: httpx.Response(
        302, headers={"location": "/report"}))
    assert tools.research_read(eid)["error"] == "redirect_limit"
    assert len(requests) == 5  # one search and four redirect responses


def test_web_pdf_uses_document_extractor_and_keeps_physical_page():
    writer = PdfWriter()
    page = writer.add_blank_page(200, 200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 100 Td (measurement 45 m) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    tools, _, eid, _ = reader(lambda _: httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=output.getvalue()))
    read = tools.research_read(eid)
    assert read["section"].startswith("page:1@0-")
    assert "measurement 45 m" in read["text"]
    assert read["web_coverage"]["searchable_sections"] == 1


def test_operator_page_limit_does_not_expand_search_response_limit():
    tools, _, eid, _ = reader(lambda _: httpx.Response(
        200, headers={"content-type": "text/plain"}, content=b"x" * 300000),
        max_bytes=1024 * 1024)
    result = tools.research_read(eid)
    assert result["passage_retrieved"]
    assert result["web_coverage"]["extraction_limited"]
    assert "text_limit" in result["limitations"]
    tools.transport = httpx.MockTransport(lambda _: httpx.Response(
        200, json={"results": [], "padding": "x" * 140000}))
    search = tools.research_search("different", "web")
    assert search["errors"]["web"] == "response_too_large"
