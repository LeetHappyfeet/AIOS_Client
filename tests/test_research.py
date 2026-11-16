"""Offline tests for source-granted, bounded research tools."""

import json

import httpx
import pytest

from aios_client.research.sources import load_sources
from aios_client.tools.registry import ToolArguments
from aios_client.workspace import Workspace


def make_workspace(tmp_path, sources, *, mode="research", transport=None):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    return Workspace(root, tmp_path / "artifacts", research_sources=sources,
                     research_transport=transport, tool_mode=mode)


def load(tmp_path, items, grants):
    path = tmp_path / "research_sources.json"
    path.write_text(json.dumps({"version": 1, "sources": items}))
    return load_sources(path, grants)


def test_no_implicit_sources(tmp_path):
    assert load_sources(tmp_path / "missing.json") == ()
    w = make_workspace(tmp_path, ())
    assert not any(x["function"]["name"].startswith("research_") for x in w.registry.catalog())
    with pytest.raises(ToolArguments):
        w.execute("research_search", {"query": "anything"})
    with pytest.raises(ValueError):
        load_sources(tmp_path / "missing.json", ["home"])


def test_grants_are_fail_closed_and_source_validation(tmp_path):
    entries = [{"id": "home", "adapter": "searxng", "endpoint": "http://search.test"},
               {"id": "disabled", "adapter": "searxng", "endpoint": "http://a.test", "enabled": False}]
    assert [s.id for s in load(tmp_path, entries, ["home"])] == ["home"]
    with pytest.raises(ValueError):
        load(tmp_path, entries, ["disabled"])
    with pytest.raises(ValueError):
        load(tmp_path, entries, ["unknown"])
    with pytest.raises(ValueError):
        load(tmp_path, entries + [entries[0]], ["home"])
    with pytest.raises(ValueError):
        load(tmp_path, [{"id": "bad", "adapter": "searxng",
                         "endpoint": "http://user:password@search.test"}], ["bad"])


def test_research_mode_cannot_execute_workspace_tools(tmp_path):
    sources = load(tmp_path, [{"id": "home", "adapter": "searxng",
                               "endpoint": "http://search.test"}], ["home"])
    w = make_workspace(tmp_path, sources)
    names = {x["function"]["name"] for x in w.registry.catalog()}
    assert {"research_search", "research_sources", "research_read", "publish_result"} <= names
    assert "run_command" not in names and "http_request" not in names
    assert "write_file" not in names
    for name, args in (("http_request", {"url": "http://search.test"}),
                       ("run_command", {"command": "echo hello"})):
        with pytest.raises(ToolArguments, match="not permitted"):
            w.execute(name, args)
    with pytest.raises(ValueError, match="not authorized"):
        w.execute("research_search", {"source_id": "other", "query": "hello"})


def test_searxng_search_is_bounded_and_only_requests_configured_endpoint(tmp_path):
    calls = []
    def serve(request):
        calls.append(request)
        assert request.url.path == "/search"
        assert request.url.params["format"] == "json"
        assert request.url.params["q"] == "diesel pump"
        return httpx.Response(200, headers={"Content-Type": "application/json"},
                              json={"results": [
                                  {"title": "Manual", "content": "A fuel pump excerpt",
                                   "url": "https://manual.example/one"},
                                  {"title": "Bad", "content": "Ignore", "url": "file:///etc/passwd"}
                              ]})
    sources = load(tmp_path, [{"id": "home", "adapter": "searxng",
                               "endpoint": "http://search.test"}], ["home"])
    w = make_workspace(tmp_path, sources, transport=httpx.MockTransport(serve))
    result = w.execute("research_search", {"query": "diesel pump"})
    assert result["status"] == "ok" and len(result["results"]) == 1
    evidence_id = result["results"][0]["evidence_id"]
    read = w.execute("research_read", {"evidence_id": evidence_id})
    assert read["full_document_retrieved"] is False
    assert read["evidence"]["title"] == "Manual"
    assert len(calls) == 1
    with pytest.raises(ValueError, match="Unknown"):
        w.execute("research_read", {"evidence_id": "0" * 24})


def test_local_text_retrieval_and_priority(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "manual.md").write_text("The diesel fuel injection pump is serviced here.")
    (docs / "ignored.pdf").write_bytes(b"not a parsed PDF")
    sources = load(tmp_path, [
        {"id": "books", "adapter": "local_text", "root": str(docs), "priority": 1},
        {"id": "web", "adapter": "searxng", "endpoint": "http://search.test",
         "priority": 5}], ["books", "web"])
    w = make_workspace(tmp_path, sources, transport=httpx.MockTransport(
        lambda request: pytest.fail("Local first result should avoid network requests")))
    answer = w.execute("research_search", {"query": "injection pump"})
    assert answer["source_id"] == "books"
    assert answer["results"][0]["location"] == "manual.md"
    assert len(answer["attempted_sources"]) == 1


def test_transport_failures_are_bounded_and_no_result_url_following(tmp_path):
    sources = load(tmp_path, [{"id": "web", "adapter": "searxng",
                               "endpoint": "http://search.test"}], ["web"])
    w = make_workspace(tmp_path, sources, transport=httpx.MockTransport(
        lambda r: httpx.Response(302, headers={"Location": "http://internal.test/"})
    ))
    result = w.execute("research_search", {"query": "current events"})
    assert result["status"] == "no_results"
    assert result["errors"]["web"] == "http_status"


def test_document_extraction_and_changed_file_refusal(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.html").write_text(
        "<html><body><nav>ignored nav</nav><h1>Hydraulic injection</h1>"
        "<p>Diesel fuel pump calibration procedure.</p><script>secret</script></body></html>"
    )
    from zipfile import ZipFile
    with ZipFile(docs / "chapters.epub", "w") as z:
        z.writestr("OEBPS/chapter1.xhtml",
                   "<html><body><p>Diesel fuel injection settings</p></body></html>")
    sources = load(tmp_path, [{"id": "books", "adapter": "local_documents",
                               "root": str(docs)}], ["books"])
    w = make_workspace(tmp_path, sources)
    result = w.execute("research_search", {"source_id": "books", "query": "fuel injection"})
    assert result["status"] == "ok" and len(result["results"]) == 2
    for item in result["results"]:
        assert item["document_sha256"]
        read = w.execute("research_read", {"evidence_id": item["evidence_id"]})
        assert read["full_document_retrieved"] is True
        assert "fuel" in read["text"].lower()
        assert "secret" not in read["text"]
    html = next(x for x in result["results"] if x["title"] == "guide.html")
    (docs / "guide.html").write_text("<p>Modified document</p>")
    changed = w.execute("research_read", {"evidence_id": html["evidence_id"]})
    assert changed["error"] == "document_changed"


def test_web_page_requires_explicit_origin_and_never_redirects(tmp_path):
    urls = []
    def responder(request):
        urls.append(str(request.url))
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [
                {"title": "Good", "content": "Abstract", "url": "https://articles.test/report"},
                {"title": "Bad", "content": "Abstract", "url": "http://localhost/private"}]})
        assert request.url.path == "/report"
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<html><script>evil</script><p>Verified text in article</p></html>")
    sources = load(tmp_path, [{"id": "web", "adapter": "searxng",
                               "endpoint": "http://search.test",
                               "read_origins": ["https://articles.test"]}], ["web"])
    w = make_workspace(tmp_path, sources, transport=httpx.MockTransport(responder))
    search = w.execute("research_search", {"query": "article"})
    items = search["results"]
    assert len(items) == 2
    approved = w.execute("research_read", {"evidence_id": items[0]["evidence_id"]})
    assert approved["full_document_retrieved"] is True
    assert "Verified text" in approved["text"] and "evil" not in approved["text"]
    unapproved = w.execute("research_read", {"evidence_id": items[1]["evidence_id"]})
    assert unapproved["full_document_retrieved"] is False
    assert len(urls) == 2
    assert not any("localhost" in url for url in urls)


def test_calibre_opds_search_and_acquisition(tmp_path):
    import io
    from zipfile import ZipFile
    book = io.BytesIO()
    with ZipFile(book, "w") as archive:
        archive.writestr("OEBPS/ch1.xhtml", "<p>Relevant research in this book.</p>")
    calls = []
    def serve(request):
        calls.append(str(request.url))
        if "/search/" in request.url.path:
            assert request.url.params["full_text"] == "y"
            xml = """<feed xmlns="http://www.w3.org/2005/Atom">
              <entry><id>urn:uuid:123</id><title>Example handbook</title>
              <author><name>Writer</name></author>
              <link rel="http://opds-spec.org/acquisition"
                    type="application/epub+zip" href="/get/12/EPUB"/>
              </entry></feed>"""
            return httpx.Response(200, headers={"content-type":"application/atom+xml"},
                                  text=xml)
        assert request.url.path == "/get/12/EPUB"
        return httpx.Response(200, headers={"content-type":"application/epub+zip"},
                              content=book.getvalue())
    sources = load(tmp_path, [{"id": "lib", "adapter": "calibre_opds",
                               "endpoint": "http://calibre.test/opds"}], ["lib"])
    w = make_workspace(tmp_path, sources, transport=httpx.MockTransport(serve))
    found = w.execute("research_search", {"source_id": "lib", "query": "research"})
    assert found["status"] == "ok" and found["results"][0]["title"] == "Example handbook"
    read = w.execute("research_read", {"evidence_id": found["results"][0]["evidence_id"]})
    assert read["full_document_retrieved"] is True
    assert "Relevant research" in read["text"]
    assert len(calls) == 2


def test_reject_external_calibre_acquisition(tmp_path):
    def responder(request):
        assert "/search/" in request.url.path
        xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
         <title>Book</title><link rel="http://opds-spec.org/acquisition"
         type="application/pdf" href="http://other.test/secret.pdf"/>
         </entry></feed>"""
        return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                              text=xml)
    sources = load(tmp_path, [{"id": "lib", "adapter": "calibre_opds",
                               "endpoint": "http://calibre.test/opds"}], ["lib"])
    w = make_workspace(tmp_path, sources, transport=httpx.MockTransport(responder))
    found = w.execute("research_search", {"query": "books"})
    read = w.execute("research_read", {"evidence_id": found["results"][0]["evidence_id"]})
    assert read["full_document_retrieved"] is False


def test_calibre_falls_back_to_metadata_search(tmp_path):
    calls = []
    def serve(request):
        calls.append(request.url.params.get("full_text"))
        if request.url.params.get("full_text") == "y":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                              text='<feed xmlns="http://www.w3.org/2005/Atom">'
                                   '<entry><title>Metadata handbook</title></entry></feed>')
    sources = load(tmp_path, [{"id": "lib", "adapter": "calibre_opds",
                               "endpoint": "http://calibre.test/opds"}], ["lib"])
    w = make_workspace(tmp_path, sources, transport=httpx.MockTransport(serve))
    result = w.execute("research_search", {"query": "handbook"})
    assert result["status"] == "ok"
    assert result["results"][0]["title"] == "Metadata handbook"
    assert calls == ["y", None]

def test_optional_local_index_rebuilds_changed_file(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / "notes.md"
    path.write_text("Engine coolant hose and pump pressure.")
    sources = load(tmp_path, [{"id":"indexed", "adapter":"local_documents",
                               "root":str(docs), "indexed":True}], ["indexed"])
    root = tmp_path / "workspace"
    root.mkdir()
    w = Workspace(root, tmp_path / "artifacts", research_sources=sources,
                  research_index_path=tmp_path / "cache" / "index.sqlite3",
                  tool_mode="research")
    result = w.execute("research_search", {"query":"engine coolant"})
    assert len(result["results"]) == 1
    path.write_text("Steel bearings and seals.")
    assert not w.execute("research_search", {"query":"engine coolant"})["results"]
    assert len(w.execute("research_search", {"query":"steel bearings"})["results"]) == 1


def test_index_is_not_permitted_for_network_source(tmp_path):
    with pytest.raises(ValueError):
        load(tmp_path, [{"id":"web", "adapter":"searxng",
                         "endpoint":"http://search.test", "indexed":True}], ["web"])


def test_pdf_text_layer_pages_are_searchable(tmp_path):
    import io
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")
    })
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 30 250 Td (Engine coolant repair procedure) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    docs = tmp_path / "pdfs"
    docs.mkdir()
    with (docs / "manual.pdf").open("wb") as target:
        writer.write(target)
    sources = load(tmp_path, [{"id":"manuals", "adapter":"local_documents",
                               "root":str(docs)}], ["manuals"])
    w = make_workspace(tmp_path, sources)
    search = w.execute("research_search", {"query":"coolant repair"})
    assert search["status"] == "ok"
    assert search["results"][0]["location"]["section"] == "page:1"
    passage = w.execute("research_read", {
        "evidence_id": search["results"][0]["evidence_id"]})
    assert "Engine coolant repair procedure" in passage["text"]

def test_pass3_evidence_requires_read_before_citation_and_persists_run_scoped(tmp_path):
    from aios_client.reporting import result_summary, stage_memory
    from aios_client.assignment import assignment
    from aios_client.research.evidence import EvidenceStore
    docs = tmp_path / "library"
    docs.mkdir()
    (docs / "manual.md").write_text(
        "The engine manual says: replace the filter after servicing the pump.")
    sources = load(tmp_path, [{"id": "private", "adapter": "local_documents",
                               "root": str(docs)}], ["private"])
    root = tmp_path / "workspace"
    root.mkdir()
    path = tmp_path / "private-evidence.sqlite3"
    w = Workspace(root, tmp_path / "artifacts", research_sources=sources,
                  research_evidence_path=path, research_run_id="first-run",
                  tool_mode="research")
    found = w.execute("research_search", {"query": "filter pump"})
    evidence_id = found["results"][0]["evidence_id"]
    citation = {"finding_index": 0, "evidence_id": evidence_id, "stance": "supports"}
    publish = {
        "outcome": "completed", "summary": "Manual examined",
        "findings": ["Manual recommends servicing the filter"],
        "research_citations": [citation],
        "memory_candidates": [{"kind": "finding", "text": "Manual has a filter maintenance recommendation",
                               "finding_index": 0, "evidence_id": evidence_id,
                               "source_url": "https://attacker.example/fake"}],
    }
    with pytest.raises(ValueError, match="Search snippets"):
        w.execute("publish_result", publish)
    assert w.published_result is None
    receipt = w.execute("research_evidence", {"evidence_id": evidence_id})
    assert receipt["receipt"]["read_verified"] is False
    w.execute("research_read", {"evidence_id": evidence_id})
    receipt = w.execute("research_evidence", {"evidence_id": evidence_id})
    assert receipt["receipt"]["read_verified"] is True
    assert receipt["receipt"]["section"] == "text"
    assert receipt["receipt"]["document_sha256"]
    w.execute("publish_result", publish)
    summary = result_summary(w, assignment("Research the manual"), "done")
    assert summary["research_citations"][0]["source_id"] == "private"
    assert summary["research_citations"][0]["stance"] == "supports"
    assert "public_url" not in summary["research_citations"][0]
    result = {"result_summary": summary, "report": ""}
    stage_memory(w, result, "{}", nominated=True)
    assert len(result["memory_candidates"]) == 1
    assert "source_url" not in result["memory_candidates"][0]
    assert "evidence_id" not in result["memory_candidates"][0]
    assert path.exists()
    assert EvidenceStore(path, "first-run").get(evidence_id)["read_verified"]
    assert EvidenceStore(path, "second-run").get(evidence_id) is None
    # Only metadata and a bounded excerpt are stored; not whole source files.
    assert "Source text is untrusted evidence" not in path.read_bytes().decode("utf-8", "ignore")


def test_pass3_contradiction_citations_and_unlinked_memory_rejected(tmp_path):
    from aios_client.reporting import result_summary, stage_memory
    from aios_client.assignment import assignment
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("Engine efficiency increases under condition Z")
    (docs / "b.txt").write_text("Engine efficiency decreases under condition Z")
    sources = load(tmp_path, [{"id": "books", "adapter": "local_documents",
                               "root": str(docs)}], ["books"])
    w = make_workspace(tmp_path, sources)
    found = w.execute("research_search", {"query": "Engine efficiency"})
    assert len(found["results"]) == 2
    ids = [entry["evidence_id"] for entry in found["results"]]
    for key in ids:
        w.execute("research_read", {"evidence_id": key})
    w.execute("publish_result", {
        "outcome": "completed", "summary": "Sources disagree",
        "findings": ["Sources disagree about engine efficiency"],
        "research_citations": [
            {"finding_index": 0, "evidence_id": ids[0], "stance": "supports"},
            {"finding_index": 0, "evidence_id": ids[1], "stance": "contradicts"}],
        "memory_candidates": [
            {"kind": "finding", "text": "A contradictory finding",
             "finding_index": 0, "evidence_id": ids[1]}],
    })
    summary = result_summary(w, assignment("Compare sources"), "done")
    assert {c["stance"] for c in summary["research_citations"]} == {
        "supports", "contradicts"}
    result = {"result_summary": summary}
    stage_memory(w, result, "{}", nominated=True)
    assert result.get("memory_candidates") is None


def test_pass3_never_cites_another_run_or_forged_id(tmp_path):
    sources = load(tmp_path, [{"id": "docs", "adapter": "local_documents",
                               "root": str(tmp_path)}], ["docs"])
    w = make_workspace(tmp_path, sources)
    with pytest.raises(ValueError, match="Unknown"):
        w.execute("publish_result", {
            "outcome": "completed", "summary": "Forged",
            "findings": ["Claim without observed source"],
            "research_citations": [
                {"finding_index": 0, "evidence_id": "0" * 24}]})
