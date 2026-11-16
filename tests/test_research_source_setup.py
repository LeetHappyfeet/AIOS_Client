"""Research sources: JSON-file round trip, UI routes and safe network probes."""
from __future__ import annotations

import json
import re
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from aios_client.config import ConfigStore
from aios_client.research.manage import (
    registry_snapshot, test_source as probe_source, update_source, write_registry,
    normalize_source_draft,
)
from aios_client.research.sources import load_sources
from aios_client.server import create_app


SEARCH = {"id": "search", "adapter": "searxng",
          "endpoint": "http://192.168.1.50:8080", "priority": 5}
LIBRARY = {"id": "library", "adapter": "calibre_opds",
           "endpoint": "http://192.168.1.60:8080/opds", "priority": 10}


def test_ui_file_round_trip_and_agent_grants(tmp_path):
    path = tmp_path / "research_sources.json"
    assert registry_snapshot(path) == {"version": 1, "sources": []}
    written = write_registry(path, {"version": 1, "sources": [SEARCH, LIBRARY]})
    assert [s["id"] for s in written["sources"]] == ["search", "library"]
    assert [s.id for s in load_sources(path, ["search", "library"])] == ["search", "library"]
    if hasattr(stat, "S_IMODE"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    changed = update_source(path, source={**SEARCH, "priority": 3})
    assert changed["sources"][0]["priority"] == 3
    assert [s.id for s in load_sources(path, ["search"])] == ["search"]


def test_config_file_edits_are_read_by_ui(tmp_path):
    path = tmp_path / "research_sources.json"
    path.write_text(json.dumps({"version": 1, "sources": [SEARCH]}))
    assert registry_snapshot(path)["sources"][0]["endpoint"] == SEARCH["endpoint"]
    path.write_text(json.dumps({"version": 1, "sources": [LIBRARY]}))
    assert registry_snapshot(path)["sources"][0]["id"] == "library"


def test_bad_config_and_grant_safety(tmp_path):
    path = tmp_path / "research_sources.json"
    with pytest.raises(ValueError, match="Duplicate"):
        write_registry(path, {"version": 1, "sources": [SEARCH, SEARCH]})
    with pytest.raises(ValueError, match="granted"):
        update_source(path, source={**SEARCH, "enabled": False},
                      granted_ids={"search"})
    write_registry(path, {"version": 1, "sources": [SEARCH, LIBRARY]})
    with pytest.raises(ValueError, match="already uses"):
        update_source(path, source={**SEARCH, "id": "library"},
                      previous_id="search")
    with pytest.raises(ValueError, match="granted"):
        update_source(path, delete_id="search", granted_ids={"search"})
    with pytest.raises(ValueError, match="granted"):
        update_source(path, source={**SEARCH, "id": "renamed"},
                      previous_id="search", granted_ids={"search"})
    with pytest.raises(ValueError):
        load_sources(path, ["nonexistent"])
    path.unlink()
    path.symlink_to(tmp_path / "unknown")
    with pytest.raises(ValueError, match="symlink"):
        registry_snapshot(path)
    with pytest.raises(ValueError, match="symlink"):
        write_registry(path, {"version": 1, "sources": [SEARCH]})


def test_source_connection_checks_are_provider_specific():
    requests = []
    def serve(req):
        requests.append(str(req.url))
        if req.url.path == "/search":
            assert req.url.params["format"] == "json"
            return httpx.Response(200, headers={"content-type": "application/json"},
                                  json={"results": []})
        assert req.url.path == "/opds"
        return httpx.Response(200, headers={"content-type": "application/atom+xml"},
                              text='<feed xmlns="http://www.w3.org/2005/Atom"/>')
    transport = httpx.MockTransport(serve)
    assert probe_source(SEARCH, transport=transport)["ok"] is True
    assert probe_source(LIBRARY, transport=transport)["ok"] is True
    assert len(requests) == 2
    assert probe_source({**SEARCH, "enabled": False}, transport=transport)["ok"] is False
    assert len(requests) == 2


def test_connection_test_rejects_redirects_and_invalid_payload():
    result = probe_source(SEARCH, transport=httpx.MockTransport(
        lambda req: httpx.Response(302, headers={"location": "http://evil.test"})))
    assert result["ok"] is False
    assert result["status_code"] == 302
    result = probe_source(LIBRARY, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "application/json"},
                                   json={"results": []})))
    assert result["ok"] is False


def test_web_ui_routes_and_persisted_agent_grants(tmp_path):
    store = ConfigStore(tmp_path / "client.json")
    app = create_app(store, ui_settings={
        "host": "127.0.0.1", "port": 8765,
        "public_url": "http://127.0.0.1:8765",
    })
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        html = client.get("/").text
        assert "Research sources" in html and "gToolMode" in html
        token = re.search(r"const token='([^']+)'", html).group(1)
        headers = {"x-client-token": token}
        assert client.post("/api/research-sources", json={"source": SEARCH}).status_code == 403
        response = client.post("/api/research-sources", json={"source": SEARCH}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["sources"][0]["id"] == "search"
        assert client.get("/api/research-sources").json()["sources"][0]["id"] == "search"
        config = store.load()
        config["aios"]["example"] = {"host": "127.0.0.1", "port": 8000}
        config["endpoints"]["model"] = {"base_url": "http://127.0.0.1:5000/v1", "model": "test"}
        config["agents"]["agent"] = {
            "username": "worker", "character_id": "Cortana",
            "aios": "example", "endpoint": "model", "workspace": "/tmp",
            "user_name": "Mia", "scope_key": "conversation",
            "routing_mode": "automatic",
            "research_sources": ["search"], "tool_mode": "research",
        }
        r = client.post("/api/config", json=config, headers=headers)
        assert r.status_code == 200, r.text
        assert store.load()["agents"]["agent"]["research_sources"] == ["search"]
        assert store.load()["agents"]["agent"]["tool_mode"] == "research"
        response = client.post("/api/research-sources", json={"delete_id": "search"},
                               headers=headers)
        assert response.status_code == 400
        assert "granted" in response.json()["error"]
        config["agents"]["agent"]["research_sources"] = ["unknown"]
        assert client.post("/api/config", json=config, headers=headers).status_code == 400


def test_friendly_names_and_lan_urls_from_setup_ui(tmp_path):
    path = tmp_path / "research_sources.json"
    calibre = update_source(path, source={
        "id": "Calibre", "adapter": "calibre_opds",
        "endpoint": "http://192.168.1.16/", "priority": 100,
    })
    assert calibre["sources"][0]["id"] == "calibre"
    assert calibre["sources"][0]["endpoint"] == "http://192.168.1.16/opds"
    searx = update_source(path, source={
        "id": "SearXNG", "adapter": "searxng",
        "endpoint": "http://192.168.1.4:8080/",
    })
    by_id = {s["id"]: s for s in searx["sources"]}
    assert by_id["searxng"]["endpoint"].rstrip("/") == "http://192.168.1.4:8080"
    assert [s.id for s in load_sources(path, ["calibre", "searxng"])] == [
        "calibre", "searxng",
    ]
    assert normalize_source_draft({
        "id": "Home Library", "adapter": "calibre_opds",
        "endpoint": "http://192.168.1.16/opds/",
    })["endpoint"] == "http://192.168.1.16/opds"


def test_provider_probe_accepts_friendly_draft_and_calibre_root_url():
    seen = []
    def serve(req):
        seen.append(str(req.url))
        return httpx.Response(
            200, headers={"Content-Type": "application/atom+xml"},
            text='<feed xmlns="http://www.w3.org/2005/Atom"/>',
        )
    result = probe_source({
        "id": "Calibre", "adapter": "calibre_opds",
        "endpoint": "http://192.168.1.16/",
    }, transport=httpx.MockTransport(serve))
    assert result["ok"]
    assert result["source_id"] == "calibre"
    assert seen == ["http://192.168.1.16/opds"]


def test_file_format_still_requires_canonical_ids(tmp_path):
    path = tmp_path / "research_sources.json"
    with pytest.raises(ValueError):
        write_registry(path, {"version": 1, "sources": [{
            "id": "Calibre", "adapter": "calibre_opds",
            "endpoint": "http://192.168.1.16/",
        }]})


def test_research_permissions_visible_outside_manual_routing():
    """Automatic chat routing must not hide source grants or tool mode."""
    from pathlib import Path
    html = (Path(__file__).parents[1] / "src" / "aios_client" / "static" / "setup.html").read_text()
    start = html.index('<div id="manualRouting" hidden>')
    permissions = html.index('<summary>Research permissions</summary>')
    http_probes = html.index('<summary>HTTP probe destinations</summary>')
    # Manual routing's final explanatory paragraph ends with the closing div.
    manual_close = html.index(
        'Never enter the operator/review credential in this client.</small></div>',
        start,
    )
    assert start < manual_close < permissions < http_probes
    assert 'id="gToolMode"' in html[permissions:http_probes]
    assert 'id="gResearchSources"' in html[permissions:http_probes]
