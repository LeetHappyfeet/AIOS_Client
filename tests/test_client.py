import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from aios_client.config import ConfigStore, aios_url
from aios_client.http import API, Model
from aios_client.journal import Journal
from aios_client.runner import Runner, run_lock
from aios_client.server import create_app
from aios_client.workspace import Workspace


def config(tmp_path):
    p = tmp_path / "repo"
    p.mkdir()
    return {
        "version": 1,
        "aios": {
            "local": {
                "host": "127.0.0.1",
                "port": 8000,
                "secret_ref": "env:TEST_AIOS_KEY",
            }
        },
        "endpoints": {
            "model": {
                "base_url": "http://localhost:5000/v1",
                "model": "fixture",
                "secret_ref": "env:TEST_MODEL_KEY",
            }
        },
        "agents": {
            "worker": {
                "username": "agent-one",
                "character_id": "Cortana",
                "instance_id": str(uuid4()),
                "session_id": str(uuid4()),
                "user_name": "human",
                "scope_key": "workspace",
                "workspace": str(p),
                "aios": "local",
                "endpoint": "model",
            }
        },
    }


def test_profiles_atomic_no_keys_and_urls(tmp_path):
    s = ConfigStore(tmp_path / "profiles.json")
    c = config(tmp_path)
    s.save(c)
    assert s.load() == c and "api_key" not in s.path.read_text()
    assert (
        aios_url({"host": "::1", "port": 9000, "scheme": "https", "base_path": "/aios"})
        == "https://[::1]:9000/aios"
    )
    c["endpoints"]["model"]["api_key"] = "DO-NOT-SAVE"
    with pytest.raises(ValueError):
        s.save(c)
    assert "DO-NOT-SAVE" not in s.path.read_text()


def test_local_setup_csrf_and_secret_export(tmp_path, monkeypatch):
    s = ConfigStore(tmp_path / "c.json")
    s.save(config(tmp_path))
    app = create_app(s)
    with TestClient(app, base_url="http://127.0.0.1:8765") as web:
        page = web.get("/")
        assert page.status_code == 200 and "AIOS Client" in page.text
        token = page.text.split("const token='")[1].split("'")[0]
        assert web.post("/api/config", json=s.load()).status_code == 403
        assert (
            web.post(
                "/api/config",
                json=s.load(),
                headers={"X-Client-Token": token, "Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            web.get("/api/config", headers={"Host": "evil.example"}).status_code == 403
        )
        assert (
            web.post(
                "/api/config", json=s.load(), headers={"X-Client-Token": token}
            ).status_code
            == 200
        )
        assert "TEST_MODEL_KEY" in web.get("/api/config").text
        assert web.get("/api/config").headers["cache-control"] == "no-store"


def test_api_prefix_credentials_and_tool_probe():
    seen = []

    def handler(r):
        seen.append(r)
        if r.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "local"}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "x",
                                    "function": {
                                        "name": "connection_test",
                                        "arguments": '{"value":"ok"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    m = Model("http://local/proxy/v1", "secret", transport=httpx.MockTransport(handler))
    assert m.models() == ["local"]
    assert m.test_tools("local")["ok"]
    assert seen[0].url.path == "/proxy/v1/models"
    assert seen[1].headers["authorization"] == "Bearer secret"
    m.close()
    bad = Model(
        "http://local/v1",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "hello"}}]
                },
            )
        ),
    )
    with pytest.raises(ValueError):
        bad.test_tools("local")
    bad.close()


def test_workspace_paths_edits_process_output_and_cleanup(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private")
    w = Workspace(root, tmp_path / "logs", ["secret-value"], command_timeout=2)
    (root / "alias").symlink_to(outside)
    for path in ("../outside", "alias"):
        with pytest.raises(ValueError):
            w.read_file(path)
    w.apply_patch("x.py", "", "", "value = 1\n")
    old = w.read_file("x.py")["sha256"]
    w.apply_patch("x.py", old, "1", "2")
    with pytest.raises(ValueError):
        w.apply_patch("x.py", old, "2", "3")
    pid = w.run_command(
        f"{sys.executable} -c \"print('secret-value');print('x'*20000)\""
    )["process_id"]
    w.processes[pid]["done"].wait(5)
    out = w.read_process(pid)
    assert out["exit_code"] == 0 and out["next_cursor"] <= 12000
    assert "secret-value" not in w.processes[pid]["log"].read_text()
    assert "[REDACTED]" in w.processes[pid]["log"].read_text()
    pid = w.run_command(f'{sys.executable} -c "import time;time.sleep(30)"')[
        "process_id"
    ]
    w.stop()
    assert w.processes[pid]["p"].poll() is not None


def test_journal_retry_and_uncertain_recovery(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.start("r", "a", "task")
    j.event("r", "tool_started", {"name": "run_command"})
    j.queue("event", "r", "observe", {"event_key": "stable"})

    class Fail:
        def request(self, *a, **k):
            raise RuntimeError("offline")

    with pytest.raises(RuntimeError):
        j.flush(Fail())
    sent = []

    class Good:
        def request(self, *a, **k):
            sent.append(k["json"])

    j.flush(Good())
    j.flush(Good())
    assert sent == [{"event_key": "stable"}]
    j.recover()
    assert j.status()[0]["status"] == "interrupted"
    with pytest.raises(ValueError):
        j.start("r", "a", "task")
    j.db.close()


def test_duplicate_process_lock(tmp_path):
    with run_lock(tmp_path / "run.lock"), pytest.raises(ValueError):
        with run_lock(tmp_path / "run.lock"):
            pass


@pytest.mark.parametrize("credential_storage", ["env", "file"])
def test_end_to_end_scripted_coding_run(tmp_path, monkeypatch, credential_storage):
    monkeypatch.setenv("TEST_AIOS_KEY", "server-secret")
    monkeypatch.setenv("TEST_MODEL_KEY", "model-secret")
    store = ConfigStore(tmp_path / "c.json")
    c = config(tmp_path)
    if credential_storage == "file":
        if sys.platform == "win32":
            pytest.skip("POSIX credential files")
        c["aios"]["local"]["secret_ref"] = store.secrets.set("a:local", "server-secret", "file")
        c["endpoints"]["model"]["secret_ref"] = store.secrets.set("e:model", "model-secret", "file")
        monkeypatch.delenv("TEST_AIOS_KEY")
        monkeypatch.delenv("TEST_MODEL_KEY")
    store.save(c)
    store = ConfigStore(store.path)  # new client process, same saved configuration
    root = Path(c["agents"]["worker"]["workspace"])
    (root / "calc.py").write_text("def add(a,b):\n    return a-b\n")
    (root / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\nclass Test(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n"
    )
    import subprocess

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    runner = Runner(store, "worker")
    runner.api.close()
    runner.model.close()
    run = str(uuid4())
    bid = str(uuid4())
    token = str(uuid4())
    from aios_client.assignment import assignment

    spec = assignment(
        {
            "objective": "Fix add and verify it",
            "required_checks": [
                {"id": "unit", "command": f"{sys.executable} -m unittest -v"}
            ],
        }
    )
    observations = []
    finished = []

    def aios(r):
        assert r.headers["authorization"] == "Bearer server-secret"
        p = r.url.path
        body = json.loads(r.content) if r.content else {}
        if p.endswith("/protocol"):
            d = {"version": 2}
        elif p.endswith("/bind"):
            d = {"binding_id": bid}
        elif p.endswith("/submit"):
            d = {"run_id": run}
        elif p.endswith("/claim"):
            d = {
                "run": {
                    "run_id": run,
                    "claim_token": token,
                    "objective": spec.objective,
                    "assignment": spec.model_dump(mode="json"),
                    "assignment_hash": spec.digest(),
                }
            }
        elif p.endswith("/context"):
            d = {
                "generation_ready": True,
                "freshness": {"source_current": True},
                "text": "IDENTITY: Cortana",
            }
        elif p.endswith("/observe"):
            observations.append(body)
            d = {"ok": True}
        elif p.endswith("/finish"):
            finished.append(body)
            d = {"ok": True}
        elif p.endswith("/renew"):
            d = {"ok": True}
        else:
            raise AssertionError(p)
        return httpx.Response(200, json=d)

    step = 0
    seen_exit = []

    def model(r):
        nonlocal step
        assert r.headers["authorization"] == "Bearer model-secret"
        body = json.loads(r.content)
        messages = body["messages"]
        assert "IDENTITY: Cortana" in messages[0]["content"]

        def tool(name, args):
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(uuid4()),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }

        if step == 0:
            m = tool("read_file", {"path": "calc.py"})
        elif step == 1:
            h = json.loads(messages[-1]["content"])["sha256"]
            m = tool(
                "apply_patch",
                {"path": "calc.py", "expected_hash": h, "old": "a-b", "new": "a+b"},
            )
        elif step == 2:
            m = tool("run_command", {"command": f"{sys.executable} -m unittest -v"})
        else:
            tools = [json.loads(x["content"]) for x in messages if x["role"] == "tool"]
            pid = next(x["process_id"] for x in tools if x.get("status") == "started")
            if tools[-1].get("output_complete") and tools[-1].get("exit_code") == 0:
                seen_exit.append(0)
                m = {"role": "assistant", "content": "Fixed add. unittest passed."}
            else:
                time.sleep(0.05)
                m = tool("read_process", {"process_id": pid})
        step += 1
        return httpx.Response(200, json={"choices": [{"message": m}]})

    runner.api = API("http://aios", runner.aios_key, transport=httpx.MockTransport(aios))
    runner.model = Model("http://model/v1", runner.model_key, transport=httpx.MockTransport(model))
    result = runner.work(spec.model_dump(mode="json"))
    assert result["status"] == "finished" and seen_exit == [0]
    assert finished[0]["result"]["evidence"]["status"] == "passed"
    assert finished[0]["result"]["task_outcome"] == "not_independently_verified"
    assert finished[0]["result"]["baseline_git"]["is_repository"] is True
    assert finished[0]["result"]["baseline_git"]["head"] is None
    assert finished[0]["result"]["baseline_git"]["head_status"] == "unborn"
    assert "Task outcome is not independently verified" in observations[-1]["text"]
    delivery = runner.journal.delivery_summary(run)
    assert delivery["by_kind"] == {"observe": 2, "finish": 1}
    assert delivery["acknowledged"] == 3 and delivery["pending"] == 0
    assert "a+b" in (root / "calc.py").read_text()
    assert finished[0]["status"] == "finished" and finished[0]["claim_token"] == token
    assert len(observations) == 2  # Task start and final verification only.
    assert all("tool" not in x["payload"] for x in observations)
    activity = runner.activity.path.read_text()
    assert "process_started" in activity and "-m unittest -v" in activity
    assert "output process=" in activity and "process_exited" in activity
    assert "delivery_summary" in activity
    assert "server-secret" not in activity and "model-secret" not in activity
    assert all(x["event_key"] for x in observations)
    runner.close()


def test_keyring_failure_never_writes_plaintext(tmp_path, monkeypatch):
    import keyring

    from aios_client.config import Secrets

    def fail(*a):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(keyring, "set_password", fail)
    with pytest.raises(ValueError, match="No usable OS credential store"):
        Secrets("fixture").set("name", "private-token")
    assert list(tmp_path.iterdir()) == []


def test_capture_cap_and_command_deadline(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    w = Workspace(root, tmp_path / "logs", command_timeout=0.3)
    w.capture_limit = 1024
    pid = w.run_command(f"{sys.executable} -c \"print('a'*100000)\"")["process_id"]
    assert w.processes[pid]["done"].wait(3)
    assert w.processes[pid]["log"].stat().st_size == 1024
    assert w.read_process(pid)["truncated"]
    pid = w.run_command(f'{sys.executable} -c "import time;time.sleep(20)"')[
        "process_id"
    ]
    assert w.processes[pid]["done"].wait(3)
    assert w.read_process(pid)["exit_code"] is not None
    w.stop()


def test_secret_redaction_preserves_json(tmp_path):
    w = Workspace(tmp_path, tmp_path / "logs", ['key"\\value'])
    (tmp_path / "x.txt").write_text('key"\\value')
    r = w.execute("read_file", {"path": "x.txt"})
    assert r["lines"][0]["text"] == "[REDACTED]"
    w.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process group test")
def test_stop_children_after_shell_exits(tmp_path):
    w = Workspace(tmp_path, tmp_path / "logs", command_timeout=2)
    pid = w.run_command("sleep 20 &")["process_id"]
    time.sleep(0.1)
    assert w.processes[pid]["p"].poll() is not None
    assert not w.processes[pid]["done"].is_set()
    w.stop_process(pid)
    assert w.processes[pid]["done"].wait(3)
    w.stop()


@pytest.mark.parametrize(
    "check_command,expected", [(None, "not_configured"), ("exit 2", "failed")]
)
def test_final_model_claim_never_overrides_host_verification(
    tmp_path, check_command, expected, monkeypatch
):
    monkeypatch.setenv("TEST_AIOS_KEY", "test-server-key")
    monkeypatch.setenv("TEST_MODEL_KEY", "test-model-key")
    from aios_client.assignment import assignment

    store = ConfigStore(tmp_path / "c.json")
    store.save(config(tmp_path))
    runner = Runner(store, "worker")
    runner.api.close()
    runner.model.close()
    spec = assignment(
        {
            "objective": "Say done",
            "required_checks": []
            if check_command is None
            else [{"id": "check", "command": check_command}],
        }
    )
    receipts = []
    observations = []

    def api(request):
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/context"):
            return httpx.Response(
                200,
                json={
                    "generation_ready": True,
                    "freshness": {"source_current": True},
                    "text": "identity",
                },
            )
        if request.url.path.endswith("/observe"):
            observations.append(body)
        if request.url.path.endswith("/finish"):
            receipts.append(body)
        return httpx.Response(200, json={"ok": True})

    runner.api = API("http://aios", transport=httpx.MockTransport(api))
    runner.model = Model(
        "http://model",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "All tests passed. Task completed successfully.",
                            }
                        }
                    ]
                },
            )
        ),
    )
    try:
        runner.run_claim(
            str(uuid4()),
            {
                "run_id": str(uuid4()),
                "claim_token": str(uuid4()),
                "objective": spec.objective,
                "assignment": spec.model_dump(mode="json"),
                "assignment_hash": spec.digest(),
            },
        )
        assert receipts[0]["result"]["evidence"]["status"] == expected
        assert receipts[0]["status"] != "succeeded"
        assert observations[-1]["text"].startswith(
            "Workspace host verification: " + expected
        )
    finally:
        runner.close()

