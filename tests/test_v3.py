import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from test_client import config

from aios_client.assignment import assignment
from aios_client.config import ConfigStore
from aios_client.http import API, APIError, Model
from aios_client.journal import Journal
from aios_client.protocol import V3_CAPABILITIES, negotiate
from aios_client.runner import Runner
from aios_client.workspace import Workspace


def test_negotiation_uses_supported_versions_and_capabilities():
    protocol = {
        "version": 2,
        "supported_versions": [2, 3],
        "capabilities": list(V3_CAPABILITIES),
    }
    assert negotiate(protocol) == 3
    assert negotiate(protocol, 2) == 2
    for missing in V3_CAPABILITIES:
        incomplete = {**protocol, "capabilities": list(V3_CAPABILITIES - {missing})}
        assert negotiate(incomplete) == 2
        with pytest.raises(ValueError):
            negotiate(incomplete, 3)
    assert negotiate({"version": 2}) == 2
    with pytest.raises(ValueError):
        negotiate({"version": 2, "capabilities": list(V3_CAPABILITIES)}, 3)


def make_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_AIOS_KEY", "worker-secret")
    monkeypatch.setenv("TEST_MODEL_KEY", "model-secret")
    store = ConfigStore(tmp_path / "c.json")
    c = config(tmp_path)
    c["agents"]["worker"].update(
        protocol_version=3,
        link_id=str(uuid4()),
        origin_instance_id=str(uuid4()),
        share_memory=True,
    )
    store.save(c)
    r = Runner(store, "worker")
    r.api.close()
    r.model.close()
    return r


class Service:
    def __init__(self, spec):
        self.bid, self.rid, self.token = [str(uuid4()) for _ in range(3)]
        self.claim = {
            "run_id": self.rid,
            "claim_token": self.token,
            "objective": spec.objective,
            "assignment": spec.model_dump(mode="json"),
            "assignment_hash": spec.digest(),
            "required_protocol_version": 3,
            "environment_link_id": str(uuid4()),
            "origin_instance_id": str(uuid4()),
        }
        self.requests = []
        self.cancelled = False
        self.reject_observe = False
        self.event_queries = []
        self.renewals = 0
        self.cancel_at = None

    def handle(self, r):
        path = r.url.path
        body = json.loads(r.content) if r.content else {}
        self.requests.append((path, body))
        if path.endswith("/protocol"):
            data = {
                "version": 2,
                "supported_versions": [2, 3],
                "capabilities": list(V3_CAPABILITIES),
            }
        elif path.endswith("/bind"):
            data = {"binding_id": self.bid}
        elif path.endswith("/claim"):
            assert body == {"worker_key": "agent-one", "protocol_version": 3}
            data = {"run": self.claim}
        elif path.endswith("/context"):
            assert "/v3/" in path and body["claim_token"] == self.token
            data = {
                "generation_ready": True,
                "freshness": {"source_current": True},
                "text": "workspace_execution identity",
            }
        elif path.endswith("/renew"):
            self.renewals += 1
            if self.cancel_at == self.renewals:
                self.cancelled = True
            data = {"ok": True, "cancel_requested": self.cancelled}
        elif path.endswith("/recall"):
            assert set(body) == {"worker_key", "claim_token", "query"}
            data = {"status": "unavailable", "hits": [], "knowledge_revision": 12}
        elif path.endswith("/observe") and self.reject_observe:
            self.cancelled = True
            return httpx.Response(409, json={})
        elif path.endswith("/events"):
            after = int(r.url.params["after"])
            self.event_queries.append(after)
            data = {
                "events": [{"sequence": 1, "kind": "started"}] if after == 0 else [],
                "next_cursor": max(after, 1),
            }
        elif r.method == "GET":
            data = {"cancel_requested": self.cancelled, "status": "running"}
        else:
            data = {"ok": True}
        return httpx.Response(200, json=data)

    def bodies(self, suffix):
        return [b for p, b in self.requests if p.endswith(suffix)]


def model_api(handler):
    def wrapped(r):
        m = handler(json.loads(r.content))
        return httpx.Response(200, json={"choices": [{"message": m}]})

    return Model("http://model", transport=httpx.MockTransport(wrapped))


def tool(name, args, call_id="one"):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def test_v3_worker_recall_progress_and_immutable_assignment(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    spec = assignment(
        {"objective": "Inspect prior attempts", "preserve_paths": ["precious.txt"]}
    )
    service = Service(spec)
    r.api = API(
        "http://aios/proxy",
        "worker-secret",
        transport=httpx.MockTransport(service.handle),
    )
    step = 0

    def model(body):
        nonlocal step
        assert spec.model_dump(mode="json") == json.loads(
            body["messages"][1]["content"]
        )
        assert "workspace_execution" in body["messages"][0]["content"]
        assert {"aios_recall", "aios_progress"} <= {
            x["function"]["name"] for x in body["tools"]
        }
        step += 1
        if step == 1:
            return tool("aios_recall", {"query": "What did I try before?"})
        if step == 2:
            assert (
                json.loads(body["messages"][-1]["content"])["status"] == "unavailable"
            )
            return tool(
                "aios_progress",
                {"kind": "blocked", "summary": "Need operator clarification"},
                "two",
            )
        return {
            "role": "assistant",
            "content": "Inspected context; clarification still needed.",
        }

    r.model = model_api(model)
    try:
        result = r.work()
        assert result["status"] == "finished"
        assert service.bodies("/recall")[0]["claim_token"] == service.token
        assert any(b["kind"] == "blocked" for b in service.bodies("/progress"))
        assert len({b["event_key"] for b in service.bodies("/progress")}) == 4
        assert (
            service.bodies("/finish")[0]["result"]["evidence"]["status"]
            == "not_configured"
        )
        claim = r.journal.claims()[0]
        assert len(service.bodies("/observe")) == 2
        assert not any("tool" in b["payload"] for b in service.bodies("/observe"))
        assert claim["claim"]["assignment_hash"] == spec.digest()
        assert claim["lease"]["claim_token"] == service.token
        assert claim["cursor"] == 1
        r.sync_events(claim)
        assert service.event_queries == [0, 1]
        assert (
            len(
                [
                    e
                    for e in r.journal.events(service.rid)
                    if e["kind"] == "remote_event"
                ]
            )
            == 1
        )
    finally:
        r.close()


@pytest.mark.parametrize("cancel_at", [1, 3, 4])
def test_cancel_before_model_or_before_tool_never_executes_shell(
    tmp_path, monkeypatch, cancel_at
):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Do something"))
    service.cancel_at = cancel_at
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    r.protocol_version = 3
    r.model = model_api(
        lambda body: tool("run_command", {"command": "echo wrong > unwanted.txt"})
    )
    try:
        r.run_claim(service.bid, service.claim)
        assert service.bodies("/finish")[0]["status"] == "cancelled"
        assert not (Path(r.profile["workspace"]) / "unwanted.txt").exists()
        assert not r.stop_event.is_set()  # Listener remains available for another run.
    finally:
        r.close()


def test_cancel_stops_owned_process(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Run a command"))
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    r.protocol_version = 3
    step = 0

    def model(body):
        nonlocal step
        step += 1
        if step == 1:
            return tool(
                "run_command",
                {"command": f'{sys.executable} -c "import time;time.sleep(30)"'},
            )
        service.cancelled = True
        return {"role": "assistant", "content": "Done"}

    r.model = model_api(model)
    try:
        r.run_claim(service.bid, service.claim)
        assert service.bodies("/finish")[0]["status"] == "cancelled"
        assert all(p["p"].poll() is not None for p in r.workspace.processes.values())
        assert any(p["cancelled"] for p in r.workspace.processes.values())
    finally:
        r.close()


def test_progress_flood_stays_bounded_and_tools_stay_local(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Inspect without flooding memory"))
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    step = 0
    receipts = []

    def model(body):
        nonlocal step
        if step:
            receipts.append(json.loads(body["messages"][-1]["content"])["reported"])
        step += 1
        if step <= 30:
            return tool("aios_progress", {"kind": "blocked", "summary": f"Reason {step}"}, str(step))
        return {"role": "assistant", "content": "Inspection complete"}

    r.model = model_api(model)
    try:
        assert r.work()["status"] == "finished"
        assert receipts.count(True) == 20 and receipts.count(False) == 10
        assert len(service.bodies("/progress")) == 23
        assert len(service.bodies("/observe")) == 2
        assert not any("tool" in b["payload"] for b in service.bodies("/observe"))
    finally:
        r.close()


def test_revoked_observation_does_not_block_cancelled_finish(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Inspect"))
    service.reject_observe = True
    r.protocol_version = 3
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    r.model = model_api(lambda b: pytest.fail("Cancelled before model"))
    try:
        r.run_claim(service.bid, service.claim)
        assert service.bodies("/finish")[0]["status"] == "cancelled"
        assert not r.journal.db.execute("SELECT * FROM outbox").fetchall()
    finally:
        r.close()


def test_source_conflict_is_preserved_for_reconciliation(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.queue("obs", "r", "workspace-client/b/runs/r/observe", {"event_key": "stable"})
    j.queue(
        "finish:r", "r", "workspace-client/b/runs/r/finish", {"status": "interrupted"}
    )

    def handler(req):
        return (
            httpx.Response(409, json={})
            if req.method == "POST"
            else httpx.Response(200, json={"status": "running"})
        )

    api = API("http://aios", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(APIError):
            j.flush(api)
        assert j.db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2
    finally:
        api.close()
        j.db.close()


def test_restart_reports_interruption_and_preserves_claim_cursor_and_finish(tmp_path):
    path = tmp_path / "j.db"
    j = Journal(path)
    meta = {
        "lease": {"worker_key": "worker", "claim_token": "token"},
        "prefix": "workspace-client/b/runs/r",
        "claim": {"assignment_hash": "hash"},
        "protocol_version": 3,
    }
    j.start("r", "worker", "task", meta)
    j.event(
        "r",
        "tool_started",
        {"name": "run_command", "arguments": {"command": "uncertain"}},
    )
    j.remote_events("r", [{"sequence": 10, "kind": "started"}], 10)
    j.db.close()
    j = Journal(path)
    try:
        j.recover()
        j.recover()
        sent = []

        class Sink:
            def request(self, method, path, json):
                sent.append(json)

        j.flush(Sink())
        assert len(sent) == 1 and sent[0]["status"] == "interrupted"
        assert sent[0]["claim_token"] == "token"
        assert j.claims()[0]["cursor"] == 10
        assert j.claims()[0]["claim"]["assignment_hash"] == "hash"
        assert j.status()[0]["status"] == "interrupted"
        with pytest.raises(ValueError):
            j.start("r", "worker", "task", meta)
        j.start(
            "r2", "worker", "task", {**meta, "prefix": "workspace-client/b/runs/r2"}
        )
        finish = {"status": "finished", "result": {"evidence": "retained"}}
        j.queue("finish:r2", "r2", "workspace-client/b/runs/r2/finish", finish)
        j.recover()
        assert (
            json.loads(
                j.db.execute("SELECT body FROM outbox WHERE id='finish:r2'").fetchone()[
                    0
                ]
            )
            == finish
        )
    finally:
        j.db.close()


def test_progress_retry_keeps_event_uuid_and_rejects_changed_content(tmp_path):
    j = Journal(tmp_path / "j.db")
    event = {
        "event_key": str(uuid4()),
        "kind": "progress",
        "summary": "Inspected source",
    }
    j.queue("progress", "r", "progress", event)
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(503 if len(seen) == 1 else 200, json={})

    api = API("http://aios", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(APIError):
            j.flush(api)
        with pytest.raises(ValueError):
            j.queue("progress", "r", "progress", {**event, "summary": "Changed"})
        j.flush(api)
        assert seen == [event, event]
    finally:
        api.close()
        j.db.close()


def test_pairing_and_worker_only_credentials(tmp_path, monkeypatch):
    c = config(tmp_path)
    agent = c["agents"]["worker"]
    agent.update(link_id=str(uuid4()), origin_instance_id=agent["instance_id"])
    with pytest.raises(ValueError, match="dedicated"):
        ConfigStore.validate(c)
    agent["origin_instance_id"] = str(uuid4())
    agent["share_memory"] = True
    ConfigStore.validate(c)
    c["aios"]["local"]["secret_ref"] = "env:AIOS_WORKSPACE_REVIEW_KEY"
    with pytest.raises(ValueError, match="worker credential"):
        ConfigStore.validate(c)
    monkeypatch.setenv("AIOS_WORKSPACE_REVIEW_KEY", "operator-private")
    w = Workspace(tmp_path, tmp_path / "logs")
    try:
        pid = w.run_command(
            f"{sys.executable} -c \"import os;print(os.getenv('AIOS_WORKSPACE_REVIEW_KEY', 'absent'))\""
        )["process_id"]
        assert w.processes[pid]["done"].wait(3)
        assert w.read_process(pid)["output"].strip() == "absent"
        w.stop()
        with pytest.raises(RuntimeError, match="stopped"):
            w.run_command("echo too late")
    finally:
        w.stop()


def test_event_pages_resume_after_restart(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    r.api = API(
        "http://aios",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "events": [
                        {"sequence": n}
                        for n in range(
                            int(req.url.params["after"]) + 1,
                            min(150, int(req.url.params["after"]) + 100) + 1,
                        )
                    ],
                    "next_cursor": min(150, int(req.url.params["after"]) + 100),
                },
            )
        ),
    )
    r.journal.start(
        "r",
        "worker",
        "task",
        {"protocol_version": 3, "v3_prefix": "workspace-client/v3/b/runs/r"},
    )
    try:
        r.sync_events(r.journal.claims()[0], drain=False)
        assert r.journal.claims()[0]["cursor"] == 100
        r.journal.db.close()
        r.journal = Journal(r.directory / "journal.sqlite3")
        r.sync_events(r.journal.claims()[0])
        assert r.journal.claims()[0]["cursor"] == 150
        assert (
            r.journal.db.execute(
                "SELECT count(*) FROM events WHERE kind='remote_event'"
            ).fetchone()[0]
            == 150
        )
    finally:
        r.close()


def test_saved_claim_cannot_be_delivered_to_changed_binding(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Inspect"))
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    r.journal.start(
        "old-run",
        "worker",
        "old task",
        {
            "binding_id": "another-binding",
            "server_url": "http://aios/",
            "prefix": "workspace-client/another-binding/runs/old-run",
            "lease": {},
        },
    )
    try:
        with pytest.raises(ValueError, match="another server/binding"):
            r.work()
        assert not service.bodies("/finish")
        assert not service.bodies("/claim")
    finally:
        r.close()


def test_linked_worker_refuses_local_operator_submission(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    r.api = API(
        "http://aios",
        transport=httpx.MockTransport(lambda req: pytest.fail("No operator requests")),
    )
    try:
        with pytest.raises(ValueError, match="operator credential"):
            r.work("Do work")
    finally:
        r.close()


def test_idle_listener_retries_transient_poll_without_execution(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Inspect"))
    claims = []

    def handler(req):
        if req.url.path.endswith("/claim"):
            claims.append(json.loads(req.content))
            if len(claims) == 1:
                return httpx.Response(503, json={})
            r.stop()
            return httpx.Response(200, json={"run": None})
        return service.handle(req)

    r.api = API("http://aios", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(r.stop_event, "wait", lambda timeout: r.stop_event.is_set())
    try:
        r.work(serve=True)
        assert len(claims) == 2
        assert not r.journal.status()
    finally:
        r.close()


def test_recall_rejects_model_selected_identity(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Inspect"))
    r.protocol_version = 3
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    step = 0

    def model(body):
        nonlocal step
        step += 1
        if step == 1:
            return tool("aios_recall", {"query": "secret", "instance_id": str(uuid4())})
        assert "error" in json.loads(body["messages"][-1]["content"])
        return {"role": "assistant", "content": "Done"}

    r.model = model_api(model)
    try:
        r.run_claim(service.bid, service.claim)
        assert not service.bodies("/recall")
    finally:
        r.close()


def test_terminal_state_and_delivery_commit_together_and_adopt_server_cancellation(
    tmp_path,
):
    path = tmp_path / "j.db"
    j = Journal(path)
    j.start("r", "worker", "task", {"prefix": "workspace-client/b/runs/r", "lease": {}})
    report = {"status": "finished", "result": {"evidence": {"status": "passed"}}}
    j.finish(
        "r", "finished", report["result"], "workspace-client/b/runs/r/finish", report
    )
    j.db.close()
    j = Journal(path)
    api = API(
        "http://aios",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "status": "cancelled",
                    "result": {"evidence": {"status": "passed"}},
                },
            )
        ),
    )
    try:
        j.recover()
        assert (
            json.loads(j.db.execute("SELECT body FROM outbox").fetchone()[0]) == report
        )
        j.flush(api)
        assert j.status()[0]["status"] == "cancelled"
        assert j.status()[0]["result"]["evidence"]["status"] == "passed"
        assert not j.db.execute("SELECT * FROM outbox").fetchall()
    finally:
        api.close()
        j.db.close()

