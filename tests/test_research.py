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
