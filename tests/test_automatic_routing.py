import json
from uuid import uuid4

import httpx
import pytest
from aios_client.config import ConfigStore
from aios_client.http import API, APIError, Model
from aios_client.runner import Runner
from aios_client.protocol import V3_CAPABILITIES
from test_client import config


def automatic(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_AIOS_KEY', 'fixture-key')
    monkeypatch.setenv('TEST_MODEL_KEY', 'fixture-model-key')
    data = config(tmp_path)
    p = data['agents']['worker']
    p.update(routing_mode='automatic')
    p.pop('instance_id'); p.pop('session_id')
    store = ConfigStore(tmp_path / 'client.json')
    store.save(data)
    return store


def protocol():
    return {'version': 2, 'supported_versions': [2, 3],
            'capabilities': sorted(V3_CAPABILITIES | {'persistent-workspaces-v1'})}


def test_profile_needs_no_chat_uuids_but_keeps_operator_boundary(tmp_path, monkeypatch):
    store = automatic(tmp_path, monkeypatch)
    data = store.load()
    ConfigStore.validate(data)
    data['agents']['worker']['link_id'] = str(uuid4())
    with pytest.raises(ValueError, match='manual links'):
        ConfigStore.validate(data)


def test_registration_identity_does_not_change_with_chat(tmp_path, monkeypatch):
    runner = Runner(automatic(tmp_path, monkeypatch), 'worker')
    seen = []
    def handler(request):
        if request.url.path.endswith('/protocol'):
            return httpx.Response(200, json=protocol())
        assert request.url.path == '/workspace-client/registrations'
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(200, json={'registration_id': str(uuid4()), 'enabled': False})
    runner.api.close()
    runner.api = API('http://aios', transport=httpx.MockTransport(handler))
    try:
        runner.binding()
        runner.profile['instance_id'] = str(uuid4())
        runner.profile['session_id'] = str(uuid4())
        runner.binding()
        assert seen[0] == seen[1]
        assert 'instance_id' not in seen[0] and 'session_id' not in seen[0]
    finally:
        runner.close()


def test_listener_uses_claim_binding_for_each_chat(tmp_path, monkeypatch):
    runner = Runner(automatic(tmp_path, monkeypatch), 'worker')
    rid = str(uuid4())
    bids = [str(uuid4()), str(uuid4())]
    runs = [str(uuid4()), str(uuid4())]
    requests, receipts = [], []
    polls = iter([{'run': None, 'status': 'needs_authorization'},
                  {'run': {'run_id': runs[0]}, 'binding_id': bids[0], 'status': 'listening'},
                  {'run': {'run_id': runs[1]}, 'binding_id': bids[1], 'status': 'listening'}])
    def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith('/protocol'):
            return httpx.Response(200, json=protocol())
        if request.url.path == '/workspace-client/registrations':
            return httpx.Response(200, json={'registration_id': rid, 'enabled': False})
        assert request.url.path == f'/workspace-client/registrations/{rid}/claim'
        return httpx.Response(200, json=next(polls))
    def run_claim(binding, claim):
        receipts.append((binding, claim['run_id']))
        if len(receipts) == 2:
            runner.stop_event.set()
    runner.api.close()
    runner.api = API('http://aios', transport=httpx.MockTransport(handler))
    monkeypatch.setattr(runner, 'run_claim', run_claim)
    monkeypatch.setattr(runner.stop_event, 'wait', lambda _: None)
    try:
        runner.work(serve=True)
        assert receipts == list(zip(bids, runs))
        assert '/workspace-client/bind' not in requests
        assert runner.listener_status == 'listening'
    finally:
        runner.close()


def test_unsupported_server_never_silently_binds_to_chat(tmp_path, monkeypatch):
    runner = Runner(automatic(tmp_path, monkeypatch), 'worker')
    runner.api.close()
    runner.api = API('http://aios', transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'version': 2})))
    try:
        with pytest.raises(ValueError, match='Update AIOS'):
            runner.binding()
    finally:
        runner.close()


def test_setup_conflict_detail_is_bounded_redacted_and_provider_errors_stay_private():
    def handler(request):
        return httpx.Response(409, json={'detail': 'Identity changed fixture-key\n' + 'x' * 1000})
    api = API('http://aios', 'fixture-key', transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(APIError) as exc:
            api.request('POST', 'workspace-client/bind', json={})
        assert '[REDACTED]' in str(exc.value) and 'fixture-key' not in str(exc.value)
        assert len(str(exc.value)) < 700
    finally:
        api.close()
    model = Model('http://model', 'fixture-key', transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(APIError) as exc:
            model.request('POST', 'workspace-client/bind', json={})
        assert 'Identity changed' not in str(exc.value)
    finally:
        model.close()
