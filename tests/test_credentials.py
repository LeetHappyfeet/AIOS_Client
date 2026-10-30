import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest
from fastapi.testclient import TestClient

from aios_client.config import ConfigStore
from aios_client.server import create_app


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX credential files")


def test_saved_keys_survive_restart_replace_and_delete(tmp_path, monkeypatch):
    # A headless keyring must not be consulted when local storage is selected.
    import keyring

    def unavailable(*args):
        raise AssertionError("keyring should not be used")

    monkeypatch.setattr(keyring, "set_password", unavailable)
    monkeypatch.setattr(keyring, "get_password", unavailable)
    store = ConfigStore(tmp_path / "client.json")
    worker = store.secrets.set("a:AIOS", "workspace-test-key", "file")
    model = store.secrets.set("e:DeepSeek", "model-test-key", "file")
    fresh = ConfigStore(store.path)
    assert fresh.secrets.get(worker) == "workspace-test-key"
    assert fresh.secrets.get(model) == "model-test-key"
    assert stat.S_IMODE(fresh.secrets.directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in fresh.secrets.directory.iterdir())
    fresh.secrets.set("e:DeepSeek", "replacement", "file")
    assert store.secrets.get(model) == "replacement"
    assert store.secrets.get(worker) == "workspace-test-key"
    fresh.secrets.delete(model)
    fresh.secrets.delete(model)  # repeated removal is harmless
    with pytest.raises(ValueError, match="missing"):
        store.secrets.get(model)


def test_local_keys_scoped_to_config_and_not_exported(tmp_path):
    a = ConfigStore(tmp_path / "one.json")
    b = ConfigStore(tmp_path / "two.json")
    ref = a.secrets.set("e:model", "do-not-export", "file")
    with pytest.raises(ValueError, match="missing"):
        b.secrets.get(ref)
    config = {"version": 1, "aios": {}, "agents": {}, "endpoints": {
        "model": {"base_url": "http://model/v1", "secret_ref": ref}
    }}
    a.save(config)
    assert "do-not-export" not in a.path.read_text()
    assert a.load() == config


@pytest.mark.parametrize("target", ["directory", "file"])
def test_insecure_permissions_rejected(tmp_path, target):
    s = ConfigStore(tmp_path / "client.json").secrets
    ref = s.set("e:model", "private", "file")
    path = s.directory if target == "directory" else next(s.directory.iterdir())
    path.chmod(0o755 if target == "directory" else 0o644)
    for operation in (lambda: s.get(ref), lambda: s.set("e:model", "new", "file"), lambda: s.delete(ref)):
        with pytest.raises(ValueError, match="owned by this user"):
            operation()


@pytest.mark.parametrize("target", ["directory", "file"])
def test_symlinks_rejected(tmp_path, target):
    s = ConfigStore(tmp_path / "client.json").secrets
    ref = s.set("e:model", "private", "file")
    path = s.directory if target == "directory" else next(s.directory.iterdir())
    outside = tmp_path / "elsewhere"
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=target == "directory")
    for operation in (lambda: s.get(ref), lambda: s.set("e:model", "new", "file"), lambda: s.delete(ref)):
        with pytest.raises(ValueError):
            operation()


def test_corrupt_or_blank_credentials_fail_without_exposing_contents(tmp_path):
    s = ConfigStore(tmp_path / "client.json").secrets
    ref = s.set("e:model", "private", "file")
    path = next(s.directory.iterdir())
    for content in ('{"secret":"private"', '42', '""'):
        path.write_text(content)
        with pytest.raises(ValueError) as error:
            s.get(ref)
        assert "private" not in str(error.value)
    with pytest.raises(ValueError, match="non-empty"):
        s.set("e:model", "  ", "file")


def test_setup_stores_reference_only_and_restart_can_use_key(tmp_path):
    store = ConfigStore(tmp_path / "client.json")
    with TestClient(create_app(store), base_url="http://127.0.0.1:8765") as web:
        page = web.get("/").text
        token = page.split("const token='")[1].split("'")[0]
        headers = {"X-Client-Token": token}
        payload = {"name": "e:DeepSeek", "value": "never-return-this", "storage": "file"}
        assert web.post("/api/secret", json=payload).status_code == 403
        assert web.get("/api/credential-storage").json() == {"local_file": True}
        result = web.post("/api/secret", json=payload, headers=headers)
        assert result.json() == {"secret_ref": "file:e:DeepSeek"}
        config = {"version": 1, "aios": {}, "agents": {}, "endpoints": {
            "DeepSeek": {"base_url": "http://model/v1", **result.json()}
        }}
        assert web.post("/api/config", json=config, headers=headers).status_code == 200
        assert "never-return-this" not in web.get("/api/config").text
        assert "never-return-this" not in web.get("/").text
    fresh = ConfigStore(store.path)
    ref = fresh.load()["endpoints"]["DeepSeek"]["secret_ref"]
    assert fresh.secrets.get(ref) == "never-return-this"


def test_existing_environment_credentials_still_work(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_EXISTING_KEY", "env-value")
    store = ConfigStore(tmp_path / "client.json")
    assert store.secrets.get("env:TEST_EXISTING_KEY") == "env-value"
    with pytest.raises(ValueError, match="password field"):
        store.secrets.get("literal-pasted-key")


@pytest.mark.skipif(not shutil.which("node"), reason="Node optional for setup JavaScript tests")
def test_setup_credential_ui():
    subprocess.run(["node", str(Path(__file__).with_name("test_setup_ui.cjs"))], check=True)
