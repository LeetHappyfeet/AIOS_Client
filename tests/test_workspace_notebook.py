import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from test_v3 import Service, make_runner, model_api, tool

from aios_client.assignment import assignment
from aios_client.http import API
from aios_client.tools.notebook import CAPABILITY, register
from aios_client.tools.registry import Registry, ToolArguments


@pytest.mark.parametrize("advertised, linked, brief_status", [
    (True, True, 200), (True, True, 503), (True, False, 200), (False, True, 200),
])
def test_worker_notebook_tools_and_resume_brief(tmp_path, monkeypatch, advertised, linked, brief_status):
    goal = str(uuid4())
    spec = assignment({"objective": "Continue the geology project", **(
        {"goal_id": goal, "attempt_id": str(uuid4())} if linked else {}
    )})
    enabled = advertised and linked
    step = 0

    class NotebookService(Service):
        def handle(self, request):
            path = request.url.path
            if "/notebook/" in path:
                body = json.loads(request.content)
                self.requests.append((path, body))
                lease = {"worker_key": "agent-one", "claim_token": self.token}
                if path.endswith("/brief"):
                    assert body == lease
                    return httpx.Response(brief_status, json={
                        "status": "retrieved", "notes": [{"content": "Read the Michigan slides next"}],
                    })
                assert path.endswith("/append")
                assert body == {**lease, "entry_type": "next_step", "content": "Inspect slide 12"}
                return httpx.Response(200, json={"status": "stored", "note_ref": "stored-checkpoint"})
            response = super().handle(request)
            if advertised and path.endswith("/protocol"):
                data = response.json()
                data["capabilities"].append(CAPABILITY)
                return httpx.Response(200, json=data)
            return response

    runner = make_runner(tmp_path, monkeypatch)
    service = NotebookService(spec)
    runner.api = API("http://aios", transport=httpx.MockTransport(service.handle))

    def model(body):
        nonlocal step
        step += 1
        names = {t["function"]["name"] for t in body["tools"]}
        assert ("aios_notebook_search" in names) == enabled
        assert ("aios_notebook_recent" in names) == enabled
        assert ("aios_notebook_append" in names) == enabled
        messages = json.dumps(body["messages"])
        if enabled and step == 1:
            assert "PROJECT NOTEBOOK RESUME BRIEF" in messages
            if brief_status == 200:
                assert "Read the Michigan slides next" in messages
            else:
                assert "unavailable" in messages
            return tool("aios_notebook_append", {"entry_type": "next_step", "content": "Inspect slide 12"})
        if enabled:
            assert "stored-checkpoint" in messages
        return {"role": "assistant", "content": "Reviewed the current project direction."}

    runner.model = model_api(model)
    try:
        assert runner.work()["status"] == "finished"
        assert len(service.bodies("/notebook/brief")) == int(enabled)
        assert len(service.bodies("/notebook/append")) == int(enabled)
        setup = next(event["body"] for event in runner.journal.events(service.rid)
                     if event["kind"] == "notebook_setup")
        if enabled:
            assert setup["status"] == ("ready" if brief_status == 200 else "degraded")
            assert setup["tools_registered"] is True
        else:
            assert setup == {"status": "skipped",
                             "reason": "no_linked_goal" if not linked else "capability_not_advertised"}
    finally:
        runner.close()


def test_research_checkpoint_uses_read_receipt_and_respects_inspection():
    calls = []
    research_evidence = Mock(return_value={"receipt": {"source_id": "calibre", "read_verified": False}})
    workspace = SimpleNamespace(
        research_tools=SimpleNamespace(research_evidence=research_evidence),
        redact=lambda value: value.replace("secret", "[REDACTED]"),
    )
    registry = Registry()
    registry.tool_mode = "research"
    register(registry, lambda op, payload: calls.append((op, payload)) or {}, workspace, "test-run")
    args = {"content": "Verify this observation secret", "evidence_id": "a" * 24}
    with pytest.raises(ValueError, match="passage read"):
        registry.execute("aios_notebook_append", args)
    with pytest.raises(ToolArguments, match="Invalid tool arguments"):
        registry.execute("aios_notebook_append", {**args, "goal_id": str(uuid4())})
    assert not calls

    research_evidence.return_value["receipt"].update(
        read_verified=True, content_scope="retrieved_passage", section="page:12",
    )
    registry.execute("aios_notebook_append", args)
    operation, note = calls.pop()
    assert operation == "append"
    text, locator = note["content"].split("\n\nRetrieved passage locator: ")
    assert text == "Verify this observation [REDACTED]"
    assert json.loads(locator)["section"] == "page:12"
    assert json.loads(locator)["run_id"] == "test-run"

    registry.read_only = True
    with pytest.raises(ToolArguments):
        registry.execute("aios_notebook_append", args)
    registry.execute("aios_notebook_search", {"query": "Michigan slides"})
    assert calls == [("search", {"query": "Michigan slides", "limit": 4})]
