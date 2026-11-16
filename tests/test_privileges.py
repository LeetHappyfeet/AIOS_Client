"""Administration opt-in, fixed probes and actual process dispatch boundaries."""
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_client import config

from aios_client.config import ConfigStore
from aios_client.server import create_app
from aios_client.tools import privileges
from aios_client.tools.platforms import ProcessBackend
from aios_client.tools.registry import ToolArguments
from aios_client.workspace import Workspace


@pytest.fixture
def nonroot(monkeypatch):
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(privileges.shutil, "which", lambda name: "/usr/bin/" + name)


def test_disabled_does_not_probe_even_when_root(monkeypatch):
    monkeypatch.setattr(privileges.subprocess, "run", lambda *a, **k: pytest.fail("disabled probe"))
    p = privileges.Privileges()
    assert p.inspect()["sudo_probe"]["status"] == "disabled"
    with pytest.raises(ValueError, match="disabled"):
        p.elevated_argv(None, ["id"], "default")


@pytest.mark.parametrize("code,output,status", [(0, b"0\n", "available"), (1, b"", "denied"), (0, b"1000\n", "denied")])
def test_fixed_probe_noninteractive(nonroot, monkeypatch, code, output, status):
    def probe(argv, **kwargs):
        assert argv == ["/usr/bin/sudo", "-n", "--", "/usr/bin/id", "-u"]
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 5
        assert kwargs["stderr"] == subprocess.DEVNULL
        return SimpleNamespace(returncode=code, stdout=output)
    monkeypatch.setattr(privileges.subprocess, "run", probe)
    assert privileges.Privileges(True).inspect()["sudo_probe"]["status"] == status


def test_probe_missing_timeout_and_root(nonroot, monkeypatch):
    p = privileges.Privileges(True)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5)
    monkeypatch.setattr(privileges.subprocess, "run", timeout)
    assert p.inspect()["sudo_probe"]["status"] == "timeout"
    monkeypatch.setattr(privileges.shutil, "which", lambda _: None)
    assert p.inspect()["sudo_probe"]["status"] == "unavailable"
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 0)
    assert p.inspect()["sudo_probe"]["status"] == "already_root"
    assert p.elevated_argv(None, [sys.executable, "-V"], "default")[0] == sys.executable


def test_elevation_dispatch_retains_actual_argv_and_exit_status(tmp_path, nonroot):
    class Backend(ProcessBackend):
        def launch(self, command, argv, shell, cwd, env, stdin):
            self.actual = (command, argv, shell)
            # Simulate sudo denial without invoking the machine's sudo policy.
            return super().launch(None, [sys.executable, "-c", "import sys;sys.exit(1)"], shell, cwd, env, stdin)
    backend, events = Backend(), []
    w = Workspace(tmp_path, tmp_path / "logs", administration_enabled=True,
                  process_backend=backend, on_event=lambda *event: events.append(event))
    try:
        result = w.execute("run_command", {"argv": ["id", "-u"], "elevated": True})
        assert backend.actual == (None, ["/usr/bin/sudo", "-n", "--", "id", "-u"], "default")
        assert result["elevated"]
        assert w.processes[result["process_id"]]["done"].wait(5)
        assert w.read_process(result["process_id"])["exit_code"] == 1
        started = next(body for kind, body in events if kind == "process_started")
        assert started["elevated"] and started["argv"][1] == "-n"
        assert w.host_info()["privileges"]["administration_enabled"]
    finally:
        w.stop()


def test_read_only_allows_fixed_probe_but_never_elevated_commands(tmp_path, nonroot, monkeypatch):
    monkeypatch.setattr(privileges.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"0\n"))
    w = Workspace(tmp_path, tmp_path / "logs", administration_enabled=True)
    w.registry.read_only = True
    try:
        assert w.execute("inspect_privileges", {})["sudo_probe"]["status"] == "available"
        with pytest.raises(ToolArguments, match="Read-only"):
            w.execute("run_command", {"argv": ["id"], "elevated": True})
        assert not w.processes
    finally:
        w.stop()


def test_elevated_shell_is_wrapped_as_one_command(nonroot):
    p = privileges.Privileges(True)
    assert p.elevated_argv("id -u && whoami", None, "bash") == [
        "/usr/bin/sudo", "-n", "--", "/usr/bin/bash", "-c", "id -u && whoami"]
    with pytest.raises(ValueError, match="default shell"):
        p.elevated_argv(None, ["id"], "bash")
    with pytest.raises(ValueError, match="sh or bash"):
        p.elevated_argv("whoami", None, "pwsh")
    with pytest.raises(ValueError, match="nonempty"):
        p.elevated_argv(None, [], "default")


def test_wrapped_process_executes_and_captures_output(tmp_path, monkeypatch):
    sudo = tmp_path / "sudo-fixture"
    sudo.write_text('#!/bin/sh\n[ "$1" = "-n" ] || exit 10\nshift\n[ "$1" = "--" ] || exit 11\nshift\nexec "$@"\n')
    sudo.chmod(0o700)
    monkeypatch.setattr(privileges.os, "geteuid", lambda: 1000, raising=False)
    original_which = privileges.shutil.which
    monkeypatch.setattr(privileges.shutil, "which", lambda name: str(sudo) if name == "sudo" else original_which(name))
    w = Workspace(tmp_path, tmp_path / "logs", administration_enabled=True)
    try:
        result = w.execute("run_command", {"command": "printf elevated-fixture", "elevated": True})
        pid = result["process_id"]
        assert w.processes[pid]["done"].wait(5)
        observed = w.read_process(pid)
        assert observed["exit_code"] == 0
        assert observed["output"] == "elevated-fixture"
    finally:
        w.stop()


def test_profile_validation_and_authenticated_ui_probe(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "client.json")
    data = config(tmp_path)
    store.save(data)
    data["agents"]["worker"]["administration_enabled"] = "true"
    with pytest.raises(ValueError, match="boolean"):
        store.save(data)
    data["agents"]["worker"]["administration_enabled"] = True
    store.save(data)
    monkeypatch.setattr(privileges.Privileges, "inspect", lambda self: {"administration_enabled": self.enabled})
    with TestClient(create_app(store), base_url="http://127.0.0.1:8765") as web:
        page = web.get("/").text
        assert 'id="gAdmin"' in page
        assert web.post("/api/privileges", json={"agent": "worker"}).status_code == 403
        token = page.split("const token='")[1].split("'")[0]
        response = web.post("/api/privileges", json={"agent": "worker"}, headers={"X-Client-Token": token})
        assert response.json() == {"administration_enabled": True}
