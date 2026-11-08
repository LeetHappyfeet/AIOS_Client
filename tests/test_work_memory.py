"""Scoped report callbacks recover evidence without running diagnostic commands."""
import json
from uuid import uuid4

import httpx
import pytest

from test_v3 import Service, make_runner, model_api, tool
from aios_client.assignment import assignment
from aios_client.http import API
from aios_client.tools.recall import RemoteTools
from aios_client.tools.registry import Registry, ToolArguments


class WorkService(Service):
    def __init__(self, spec, *, advertised=True, failure=None):
        super().__init__(spec)
        self.advertised, self.failure = advertised, failure
        self.previous = str(uuid4())
        self.revision = 'a' * 64

    def handle(self, request):
        path = request.url.path
        if path.endswith(('/work-history', '/work-result')):
            body = json.loads(request.content)
            self.requests.append((path, body))
            assert body['worker_key'] == 'agent-one' and body['claim_token'] == self.token
            if self.failure:
                return httpx.Response(self.failure, json={})
            if path.endswith('/work-history'):
                assert set(body) == {'worker_key', 'claim_token'}
                result = {'status': 'retrieved', 'runs': [{
                    'run_id': self.previous, 'execution_status': 'finished',
                    'revision': self.revision, 'reported_at': '2026-11-01T17:15:12Z',
                    'summary': 'HTTPS succeeded at the time of that run',
                    'limitations': ['IPv6 untested']}]}
            else:
                assert body['run_id'] == self.previous
                assert 'instance_id' not in body and 'character_id' not in body
                cursor = body['cursor']
                if cursor:
                    assert body['revision'] == self.revision
                result = {'status': 'retrieved', 'run_id': self.previous,
                          'section': body['section'], 'text': 'historical evidence',
                          'revision': self.revision, 'next_cursor': 100 if cursor == 0 else None,
                          'evidence_source': 'worker-supplied', 'not_goal_completion': True}
            return httpx.Response(200, json=result)
        response = super().handle(request)
        if path.endswith('/protocol') and self.advertised:
            return httpx.Response(200, json={**response.json(), 'capabilities': [
                *response.json()['capabilities'], 'work-evidence-v1']})
        return response


@pytest.mark.parametrize('advertised', [False, True])
def test_worker_recovers_prior_reports_without_execution(tmp_path, monkeypatch, advertised):
    runner = make_runner(tmp_path, monkeypatch)
    spec = assignment('Read-only: use prior connectivity reports to answer what is already known')
    service = WorkService(spec, advertised=advertised)
    runner.api = API('http://aios', transport=httpx.MockTransport(service.handle))
    step = 0

    def model(body):
        nonlocal step
        step += 1
        names = {t['function']['name'] for t in body['tools']}
        if not advertised:
            assert 'aios_work_history' not in names and 'aios_read_work_result' not in names
        else:
            assert {'aios_work_history', 'aios_read_work_result'} <= names
            if step == 1:
                brief = next(m for m in body['messages'] if m.get('content', '').startswith('PRIOR WORK EVIDENCE:'))
                assert brief['role'] == 'user' and service.previous in brief['content']
                assert 'IPv6 untested' in brief['content'] and len(brief['content']) < 3300
                return tool('aios_work_history', {}, 'history')
            if step == 2:
                return tool('aios_read_work_result', {'run_id': service.previous, 'section': 'report', 'limit': 100}, 'page1')
            if step == 3:
                page = json.loads(body['messages'][-1]['content'])
                return tool('aios_read_work_result', {'run_id': service.previous, 'section': 'report',
                    'limit': 100, 'cursor': page['next_cursor'], 'revision': page['revision']}, 'page2')
        return {'role': 'assistant', 'content': json.dumps({'outcome': 'partial',
            'summary': 'Used prior evidence; no new probes', 'evidence': [],
            'limitations': ['Historical report only'], 'artifacts': []})}

    runner.model = model_api(model)
    try:
        result = runner.work()
        assert result['status'] == 'finished'
        if advertised:
            assert len(service.bodies('/work-history')) == 2  # startup brief + explicit lookup
            assert len(service.bodies('/work-result')) == 2
        else:
            assert not service.bodies('/work-history') and not service.bodies('/work-result')
        state = runner.journal.state(service.rid)
        assert not state['context']['host_state'].get('partial_findings')
        assert result['result']['result_summary']['findings'] == []
        tools = [e for e in runner.journal.events(service.rid) if e['kind'] == 'tool_call']
        assert not any('run_command' in str(e) for e in tools)
        assert not any('tool' in b.get('payload', {}) for b in service.bodies('/observe'))
    finally:
        runner.close()


@pytest.mark.parametrize('failure,expected', [(503, 'finished'), (409, 'failed')])
def test_startup_unavailability_does_not_mask_lost_authority(tmp_path, monkeypatch, failure, expected):
    runner = make_runner(tmp_path, monkeypatch)
    service = WorkService(assignment('Read-only: inspect prior evidence'), failure=failure)
    runner.api = API('http://aios', transport=httpx.MockTransport(service.handle))

    def model(body):
        assert '"status": "unavailable"' in body['messages'][-1]['content']
        return {'role': 'assistant', 'content': 'Prior work is temporarily unavailable.'}

    runner.model = model_api(model)
    try:
        assert runner.work()['status'] == expected
    finally:
        runner.close()


def test_work_tools_reject_identity_and_require_revision_before_transport():
    calls = []
    tools = RemoteTools(lambda q: {}, lambda *a: True,
                        work_history=lambda: {}, work_result=lambda query: calls.append(query) or {})
    registry = Registry()
    registry.read_only = True
    tools.register(registry)
    for arguments in ({'run_id': str(uuid4()), 'instance_id': str(uuid4())},
                      {'run_id': str(uuid4()), 'limit': 4001}):
        with pytest.raises(ToolArguments):
            registry.execute('aios_read_work_result', arguments)
    with pytest.raises(ValueError, match='revision'):
        registry.execute('aios_read_work_result', {'run_id': str(uuid4()), 'cursor': 100})
    assert calls == []


@pytest.mark.parametrize("publish", [False, True])
def test_compact_brief_and_memory_use_existing_finish_only(tmp_path, monkeypatch, publish):
    runner = make_runner(tmp_path, monkeypatch)
    spec = assignment("Read-only: choose the next TLS probe")
    note = {"kind": "attempt", "text": "The gateway rejected the staging certificate.",
            "finding_index": 0}

    class CompactService(WorkService):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path.endswith("/protocol"):
                data = response.json()
                data["capabilities"].append("workspace-memory-v1")
                return httpx.Response(200, json=data)
            if request.url.path.endswith("/context"):
                assert json.loads(request.content)["compact"] is True
                return httpx.Response(200, json={
                    **response.json(), "format": "workspace-memory-v1",
                    "text": "AIOS WORKER BRIEF\nPrior attempt: inspect the gateway certificate first.",
                })
            return response

    service = CompactService(spec)
    runner.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    step = 0

    def model(body):
        nonlocal step
        step += 1
        assert "AIOS WORKER BRIEF" not in body["messages"][0]["content"]
        assert any(m["role"] == "user" and "inspect the gateway certificate first" in m["content"]
                   for m in body["messages"] if isinstance(m.get("content"), str))
        assert {"aios_recall", "aios_work_history", "aios_read_work_result"} <= {
            t["function"]["name"] for t in body["tools"]}
        result = {"outcome": "blocked", "summary": "Gateway certificate is the next probe",
                  "limitations": [], "memory_candidates": [note]}
        if publish and step == 1:
            return tool("publish_result", {**result, "findings": ["TLS handshake failed"]}, "result")
        return {"role": "assistant", "content": json.dumps({
            **result, "evidence": ["TLS handshake failed"],
        })}

    runner.model = model_api(model)
    try:
        assert runner.work()["status"] == "finished"
        assert not service.bodies("/observe") and not service.bodies("/work-history")
        finish, = service.bodies("/finish")
        assert finish["result"]["memory_candidates"] == [note]
        assert "memory_candidates" not in finish["result"]["report"]
        assert finish["result"]["result_summary"]["findings"] == ["TLS handshake failed"]
        assert step == (2 if publish else 1)
    finally:
        runner.close()
