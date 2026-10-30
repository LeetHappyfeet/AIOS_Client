import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import socket
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from aios_client import cli
from aios_client.config import ConfigStore
from aios_client.diagnostics import doctor
from aios_client.server import create_app
from aios_client.service import install_service, systemd_quote
from aios_client.ui_settings import UISettings, password_hash, validate_ui, verify_password
from test_client import config


PASSWORD = "fixture-ui-password"


def remote_settings():
    return {"host": "0.0.0.0", "port": 8765,
            "public_url": "http://192.0.2.10:8765", "password_hash": password_hash(PASSWORD)}


def test_remote_password_and_origin_required(tmp_path):
    with pytest.raises(ValueError, match="requires a password"):
        create_app(ConfigStore(tmp_path / "client.json"), ui_settings={"host": "0.0.0.0", "public_url": "http://192.0.2.10:8765"})
    with pytest.raises(ValueError, match="requires a password"):
        validate_ui({"host": "127.0.0.1", "public_url": "https://proxy.example"})
    settings = remote_settings()
    app = create_app(ConfigStore(tmp_path / "client.json"), ui_settings=settings)
    with TestClient(app, base_url=settings["public_url"]) as web:
        for route in ("/", "/api/config", "/api/status", "/api/installation", "/api/credential-storage"):
            assert web.get(route).status_code == 401
        assert web.get("/", auth=("admin", "wrong")).status_code == 401
        page = web.get("/", auth=("admin", PASSWORD))
        assert page.status_code == 200
        assert PASSWORD not in page.text
        assert settings["password_hash"]["digest"] not in page.text
        token = page.text.split("const token='")[1].split("'")[0]
        saved = {"version": 1, "aios": {}, "endpoints": {}, "agents": {}}
        assert web.post("/api/config", json=saved, auth=("admin", PASSWORD)).status_code == 403
        headers = {"X-Client-Token": token, "Origin": settings["public_url"]}
        assert web.post("/api/config", json=saved, headers=headers, auth=("admin", PASSWORD)).status_code == 200
        assert web.post("/api/config", json=saved, headers={**headers, "Origin": "http://evil.example"}, auth=("admin", PASSWORD)).status_code == 403
        assert web.get("/api/config", auth=("admin", PASSWORD), headers={"Host": "evil.example"}).status_code == 403
        assert "password_hash" not in web.get("/api/config", auth=("admin", PASSWORD)).text
        installed = web.get("/api/installation", auth=("admin", PASSWORD)).json()
        assert installed["browser_url"] == settings["public_url"]


def test_remote_login_throttled(tmp_path):
    settings = remote_settings()
    with TestClient(create_app(ConfigStore(tmp_path / "c.json"), ui_settings=settings), base_url=settings["public_url"]) as web:
        for _ in range(5):
            assert web.get("/", auth=("admin", "bad")).status_code == 401
        response = web.get("/", auth=("admin", "bad"))
        assert response.status_code == 429 and response.headers["Retry-After"] == "30"


def test_password_persistence_and_validation(tmp_path):
    settings = remote_settings()
    ui = UISettings(tmp_path / "client.json")
    ui.save(settings)
    assert PASSWORD not in ui.path.read_text()
    saved = UISettings(tmp_path / "client.json").load()
    assert verify_password(PASSWORD, saved["password_hash"])
    assert not verify_password("incorrect", saved["password_hash"])
    if os.name == "posix":
        assert ui.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="12"):
        password_hash("short")
    for url in ("http://0.0.0.0:8765", "http://example.test/prefix", "http://u:p@example.test"):
        with pytest.raises(ValueError):
            validate_ui({**settings, "public_url": url})


def test_configure_ui_and_setup_reuse_saved_settings(tmp_path, monkeypatch):
    path = tmp_path / "client.json"
    monkeypatch.setattr("getpass.getpass", lambda *_: PASSWORD)
    monkeypatch.setattr(sys, "argv", ["aios-client", "--config", str(path), "configure-ui", "--host", "0.0.0.0", "--public-url", "http://192.0.2.10:9001"])
    cli.main()
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(sys, "argv", ["aios-client", "--config", str(path), "setup"])
    cli.main()
    assert seen["host"] == "0.0.0.0" and seen["port"] == 9001
    assert seen["proxy_headers"] is False


def test_doctor_readonly_no_generation_without_opt_in(tmp_path, monkeypatch):
    import aios_client.diagnostics as diagnostics
    store = ConfigStore(tmp_path / "client.json")
    c = config(tmp_path)
    monkeypatch.setenv("TEST_AIOS_KEY", "workspace-secret-fixture")
    monkeypatch.setenv("TEST_MODEL_KEY", "model-secret-fixture")
    store.save(c)
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("protocol"):
            return httpx.Response(200, json={"version": 2})
        if request.url.path.endswith("instances"):
            return httpx.Response(200, json={"instances": [c["agents"]["worker"]]})
        if request.url.path.endswith("models"):
            return httpx.Response(200, json={"data": [{"id": "fixture"}]})
        raise AssertionError("No generation or mutation should occur")
    from aios_client.http import API, Model
    monkeypatch.setattr(diagnostics, "API", lambda *a, **k: API(*a, **k, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(diagnostics, "Model", lambda *a, **k: Model(*a, **k, transport=httpx.MockTransport(handler)))
    result = doctor(store, "worker")
    assert result["ok"]
    assert all(method == "GET" for method, _ in calls)
    assert "workspace-secret-fixture" not in json.dumps(result)
    assert "model-secret-fixture" not in json.dumps(result)
    assert any(x["status"] == "skipped" for x in result["checks"])


def test_connection_errors_have_actionable_hints(tmp_path, monkeypatch):
    import aios_client.server as server
    from aios_client.http import APIError
    class FailingAPI:
        def __init__(self, *a): pass
        def request(self, *a): raise APIError(401, "GET", "workspace-client/protocol")
        def close(self): pass
    monkeypatch.setattr(server, "API", FailingAPI)
    with TestClient(create_app(ConfigStore(tmp_path / "c.json")), base_url="http://127.0.0.1:8765") as web:
        token = web.get("/").text.split("const token='")[1].split("'")[0]
        r = web.post("/api/test-aios", json={"profile": {"host": "127.0.0.1"}}, headers={"X-Client-Token": token})
        assert r.status_code == 502
        assert "Credential rejected" in r.json()["hint"]


@pytest.mark.skipif(os.name != "posix", reason="Linux user services")
def test_service_file_quoted_and_does_not_start_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sys, "platform", "linux")
    store = ConfigStore(tmp_path / "config space" / "client.json")
    store.save({"version": 1, "aios": {}, "endpoints": {}, "agents": {}})
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    unit = install_service(store)
    body = unit.read_text()
    assert '"setup"' in body and str(store.path) in body and "UMask=0077" in body
    assert not calls
    assert install_service(store) == unit
    assert systemd_quote('/home/a % $ "') == '"/home/a %% $$ \\""'
    with pytest.raises(ValueError):
        systemd_quote("bad\nvalue")


def test_saved_remote_ui_starts_a_real_listener(tmp_path):
    # No AIOS/model service is contacted. Exercise uvicorn with saved UI settings.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    settings = {**remote_settings(), "port": port, "public_url": f"http://127.0.0.1:{port}"}
    config_path = tmp_path / "client.json"
    UISettings(config_path).save(settings)
    process = subprocess.Popen([sys.executable, "-m", "aios_client.cli", "--config", str(config_path), "setup"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        with httpx.Client(base_url=settings["public_url"], trust_env=False, timeout=1) as web:
            for _ in range(100):
                try:
                    response = web.get("/")
                    break
                except httpx.TransportError:
                    if process.poll() is not None:
                        pytest.fail(process.stderr.read())
                    time.sleep(0.05)
            else:
                pytest.fail("UI listener did not start")
            assert response.status_code == 401
            assert web.get("/api/config", auth=("admin", PASSWORD)).status_code == 200
    finally:
        process.terminate()
        process.communicate(timeout=10)
