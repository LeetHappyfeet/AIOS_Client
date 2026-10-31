import json

import httpx
import pytest

from test_runtime import ScriptedModel, call, make_session
from test_v3 import Service, make_runner, model_api
from aios_client.assignment import assignment, requires_read_only
from aios_client.http import API, Model
from aios_client.tools.registry import ToolArguments
from aios_client.workspace import Workspace


def test_provider_diagnostics_and_configurable_output_budget():
    def handle(request):
        assert json.loads(request.content)["max_tokens"] == 8192
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "{"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292, "unexpected": "secret"},
        })
    model = Model("http://model", max_tokens=8192, transport=httpx.MockTransport(handle))
    try:
        response = model.completion("fixture", [], [])
        assert response["_diagnostics"] == {"finish_reason": "length", "max_tokens": 8192,
            "usage": {"prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292}}
    finally:
        model.close()


def test_truncated_batch_executes_nothing_and_recovers(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    w = Workspace(root, tmp_path / "artifacts", ["private-key"])
    calls = 0
    def model(messages, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Even a syntactically complete call in a truncated batch is rejected.
            return {**call("apply_patch", {"path": "wrong", "expected_hash": "", "old": "", "new": "private-key"}),
                    "_diagnostics": {"finish_reason": "length", "max_tokens": 128, "usage": {"completion_tokens": 128}}}
        assert not (root / "wrong").exists()
        assert "executed no tools" in messages[-1]["content"]
        assert all(not m.get("tool_calls") for m in messages)
        return {"role": "assistant", "content": "No change was made."}
    session, journal = make_session(w, tmp_path, ScriptedModel(model))
    try:
        assert session.run() == "No change was made."
        events = journal.events("run")
        assert any(e["kind"] == "model_response_rejected" for e in events)
        assert "private-key" not in json.dumps(events)
        assert "[REDACTED]" in json.dumps(events)
        assert not any(e["kind"] == "tool_started" for e in events)
    finally:
        w.stop()
        journal.db.close()


def test_malformed_final_json_gets_a_bounded_recovery(tmp_path):
    w = Workspace(tmp_path, tmp_path / "artifacts")
    step = 0
    def model(messages, tools):
        nonlocal step
        step += 1
        return {"role": "assistant", "content": '{"outcome":"partial"' if step == 1 else
                '{"outcome":"partial","evidence":[],"limitations":["No probes run"]}'}
    session, journal = make_session(w, tmp_path, ScriptedModel(model))
    try:
        assert json.loads(session.run())["outcome"] == "partial"
        assert step == 2
    finally:
        w.stop()
        journal.db.close()


def test_old_readonly_assignment_with_file_contract_fails_before_execution(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment({"objective": "Read-only survey", "expected_outputs": [{"path": "report.json"}]}))
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    r.model = model_api(lambda body: pytest.fail("Conflicting assignment must not reach the model"))
    try:
        run = r.work()
        assert run["status"] == "failed"
        assert "conflicts with expected output" in run["result"]["error"]
        assert not (r.workspace.root / "report.json").exists()
        assert not r.workspace.processes
    finally:
        r.close()


def test_read_only_inspection_blocks_forged_tools_and_shell(tmp_path, monkeypatch):
    w = Workspace(tmp_path, tmp_path / "artifacts")
    w.registry.read_only = True
    monkeypatch.setattr(w.process_manager.backend, "launch", lambda *a: pytest.fail("No process may launch"))
    try:
        names = {t["function"]["name"] for t in w.registry.catalog()}
        assert {"inspect_environment", "read_file", "publish_result"} <= names
        assert not {"apply_patch", "run_command", "write_process", "git_status", "http_request"} & names
        for tool, args in (("apply_patch", {}), ("run_command", {"command": "touch wrong"}), ("git_status", {})):
            with pytest.raises(ToolArguments, match="Read-only"):
                w.execute(tool, args)
        result = w.execute("inspect_environment", {})
        assert result["cwd"] == str(tmp_path)
        assert result["sudo_privileges"] == "not tested"
        assert "python3" in result["tools"]
        assert result["limitations"] and result["directories"]
        assert not (tmp_path / "wrong").exists()
    finally:
        w.stop()


def test_failed_run_preserves_partial_observations_and_diagnostics(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Read-only inspection; no file creation or modification"))
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    count = 0
    def model(body):
        nonlocal count
        count += 1
        if count == 1:
            return call("inspect_environment", {})
        return {"role": "assistant", "content": ""}
    r.model = model_api(model)
    try:
        run = r.work()
        assert run["status"] == "failed"
        result = run["result"]
        assert "recovery limit reached" in result["error"]
        assert result["model_diagnostics"]["max_tokens"] == 4096
        assert result["result_summary"]["outcome"] == "partial"
        assert "inspect_environment" in result["result_summary"]["findings"][0]
        assert len(r.workspace.processes) == 0
        assert len(service.bodies("/finish")) == 1
        assert count == 4  # inspection, two recoveries, final rejection
    finally:
        r.close()


def test_malformed_tool_then_empty_response_can_return_direct_report(tmp_path, monkeypatch):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Read-only environment survey"))
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    count = 0
    def model(body):
        nonlocal count
        count += 1
        if count == 1:
            return call("inspect_environment", {})
        if count == 2:
            response = call("apply_patch", {})
            response["tool_calls"][0]["function"]["arguments"] = '{"new":"unfinished'
            return response
        if count == 3:
            assert "JSONDecodeError" in body["messages"][-1]["content"]
            return {"role": "assistant", "content": ""}
        return {"role": "assistant", "content": json.dumps({"outcome": "partial", "summary": "Inspected environment.",
            "evidence": ["Observed host metadata"], "limitations": ["sudo privileges not tested"]})}
    r.model = model_api(model)
    try:
        result = r.work()
        assert result["status"] == "finished"
        assert result["result"]["result_summary"]["outcome"] == "partial"
        assert result["result"]["evidence"]["change_count"] == 0
        assert len(r.workspace.processes) == 0
    finally:
        r.close()


@pytest.mark.parametrize("text", ["read-only survey", "read only inspection", "no file creation or modification",
    "Examine without creating or modifying any files", "Do not create or modify files"])
def test_explicit_inspection_constraints(text):
    assert requires_read_only(text)
    assert not requires_read_only("Create a calculator and test it")
