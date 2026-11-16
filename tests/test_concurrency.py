"""Multi-profile execution keeps filesystem, inference and lease ownership separate."""
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest

from aios_client import coordination
from aios_client.config import ConfigStore
from aios_client.http import API
from aios_client.runner import Runner
from aios_client.server import Manager
from test_client import config
from test_automatic_routing import automatic, protocol


@pytest.fixture
def locks(tmp_path, monkeypatch):
    directory = tmp_path / 'locks'
    directory.mkdir()
    monkeypatch.setattr(coordination, 'coordination_dir', lambda: directory)
    return directory


def test_environment_identity_survives_repeated_reads(locks):
    first = coordination.environment_id()
    assert coordination.environment_id() == first
    assert str(__import__('uuid').UUID(first)) == first


def test_shared_and_nested_workspaces_exclude_each_other(tmp_path, locks):
    child = tmp_path / 'project' / 'child'
    child.mkdir(parents=True)
    for first, second in ((child, child), (child.parent, child), (child, child.parent)):
        with coordination.execution_slot(first) as acquired:
            assert acquired
            with coordination.execution_slot(second) as other:
                assert not other
        with coordination.execution_slot(second) as other:
            assert other


def test_independent_roots_and_host_administration(tmp_path, locks):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(); b.mkdir()
    with coordination.execution_slot(a) as first:
        assert first
        with coordination.execution_slot(b) as second:
            assert second == (os.name != 'nt')
        with coordination.execution_slot(b, administration=True) as admin:
            assert not admin
    with coordination.execution_slot(a, administration=True) as admin:
        assert admin
        with coordination.execution_slot(b) as second:
            assert not second


def test_exception_releases_all_locks(tmp_path, locks):
    with pytest.raises(RuntimeError):
        with coordination.execution_slot(tmp_path) as acquired:
            assert acquired
            raise RuntimeError('execution failed')
    with coordination.execution_slot(tmp_path) as acquired:
        assert acquired


def test_lock_is_shared_with_other_client_processes(tmp_path, locks):
    script = '''
import sys
from pathlib import Path
from aios_client import coordination
coordination.coordination_dir = lambda: Path(sys.argv[1])
with coordination.execution_slot(sys.argv[2]) as acquired:
    print(acquired)
'''
    def probe():
        return subprocess.check_output([sys.executable, '-c', script, str(locks), str(tmp_path)], text=True).strip()
    with coordination.execution_slot(tmp_path):
        assert probe() == 'False'
    assert probe() == 'True'


def test_manager_runs_two_profiles_and_reuses_endpoint_gate(tmp_path, monkeypatch):
    data = config(tmp_path)
    data['agents']['second'] = {**data['agents']['worker'], 'character_id': 'Other'}
    store = ConfigStore(tmp_path / 'config.json')
    store.save(data)
    class FakeRunner:
        def __init__(self, store, agent, gates):
            self.gate = gates['model']
            self.stopped = threading.Event()
        def work(self, *args, **kwargs):
            self.stopped.wait(5)
        def stop(self):
            self.stopped.set()
        def close(self):
            pass
    monkeypatch.setattr('aios_client.server.Runner', FakeRunner)
    manager = Manager(store)
    try:
        manager.start('worker', None, serve=True)
        manager.start('second', None, serve=True)
        assert all(thread.is_alive() for _, thread in manager.running.values())
        a, b = [runner.gate for runner, _ in manager.running.values()]
        assert a is b
        assert a.acquire(blocking=False)
        assert not b.acquire(blocking=False)
        a.release()
        with pytest.raises(ValueError, match='already running'):
            manager.start('worker', None)
        manager.stop('worker')
        manager.running['worker'][1].join(1)
        assert not manager.running['worker'][1].is_alive()
        assert manager.running['second'][1].is_alive()
    finally:
        manager.close()


def test_two_characters_register_same_environment_without_chat_ids(tmp_path, monkeypatch, locks):
    store = automatic(tmp_path, monkeypatch)
    data = store.load()
    data['agents']['second'] = {**data['agents']['worker'], 'character_id': 'Other'}
    store.save(data)
    registrations = []
    def handler(request):
        if request.url.path.endswith('/protocol'):
            result = protocol()
            result['capabilities'].append('shared-environments-v1')
            return httpx.Response(200, json=result)
        registrations.append(json.loads(request.content))
        return httpx.Response(200, json={'registration_id': str(uuid4()), 'enabled': False})
    for name in ('worker', 'second'):
        runner = Runner(store, name)
        runner.api.close()
        runner.api = API('http://aios', transport=httpx.MockTransport(handler))
        try:
            runner.binding()
        finally:
            runner.close()
    a, b = registrations
    assert a['environment_id'] == b['environment_id']
    assert a['workspace_key'] != b['workspace_key']
    assert all('session_id' not in r and 'instance_id' not in r for r in registrations)


def test_waiting_listener_heartbeats_without_claiming_task(tmp_path, monkeypatch, locks):
    runner = Runner(automatic(tmp_path, monkeypatch), 'worker')
    rid = str(uuid4())
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/protocol'):
            result = protocol()
            result['capabilities'].append('shared-environments-v1')
            return httpx.Response(200, json=result)
        if request.url.path.endswith('/registrations'):
            return httpx.Response(200, json={'registration_id': rid, 'enabled': True})
        assert request.url.path == f'/workspace-client/registrations/{rid}/heartbeat'
        return httpx.Response(200, json={'status': 'listening'})
    runner.api.close()
    runner.api = API('http://aios', transport=httpx.MockTransport(handler))
    @contextmanager
    def busy(*args):
        yield False
    monkeypatch.setattr('aios_client.runner.execution_slot', busy)
    monkeypatch.setattr(runner.stop_event, 'wait', lambda _: runner.stop())
    try:
        runner.work(serve=True)
        assert runner.listener_status == 'waiting_workspace'
        assert calls[-1].endswith('/heartbeat')
        assert not any(path.endswith('/claim') for path in calls)
    finally:
        runner.close()


def test_two_live_listeners_take_turns_and_keep_their_chat_bindings(tmp_path, monkeypatch, locks):
    store = automatic(tmp_path, monkeypatch)
    data = store.load()
    data['agents']['second'] = {**data['agents']['worker'], 'character_id': 'Other'}
    store.save(data)
    runners = [Runner(store, name) for name in ('worker', 'second')]
    release, started, waiting = threading.Event(), threading.Event(), threading.Event()
    executed, errors = [], []
    bids = [str(uuid4()), str(uuid4())]
    threads = []
    for index, runner in enumerate(runners):
        def handler(request, index=index):
            if request.url.path.endswith('/protocol'):
                result = protocol()
                result['capabilities'].append('shared-environments-v1')
                return httpx.Response(200, json=result)
            if request.url.path.endswith('/registrations'):
                return httpx.Response(200, json={'registration_id': str(uuid4()), 'enabled': True})
            if request.url.path.endswith('/heartbeat'):
                assert index == 1
                waiting.set()
                return httpx.Response(200, json={'status': 'listening'})
            assert request.url.path.endswith('/claim')
            if index == 1:
                assert release.is_set(), 'Second listener claimed while first owned the workspace'
            return httpx.Response(200, json={'run': {'run_id': str(uuid4())}, 'binding_id': bids[index], 'status': 'listening'})
        runner.api.close()
        runner.api = API('http://aios', transport=httpx.MockTransport(handler))
        def execute(bid, claim, index=index, runner=runner):
            executed.append((runner.profile['character_id'], bid))
            if index == 0:
                started.set()
                assert release.wait(5)
            runner.stop()
        monkeypatch.setattr(runner, 'run_claim', execute)
        def work(runner=runner):
            try:
                runner.work(serve=True)
            except BaseException as exc:
                errors.append(exc)
        threads.append(threading.Thread(target=work))
    try:
        threads[0].start()
        assert started.wait(5)
        threads[1].start()
        assert waiting.wait(5)
        assert executed == [('Cortana', bids[0])]
        release.set()
        for thread in threads:
            thread.join(5)
        assert not errors and all(not thread.is_alive() for thread in threads)
        assert executed == [('Cortana', bids[0]), ('Other', bids[1])]
    finally:
        release.set()
        for runner in runners:
            runner.stop()
        for thread in threads:
            if thread.ident:
                thread.join(5)
        for runner in runners:
            runner.close()


def test_manual_submission_reports_queued_run_when_workspace_is_busy(tmp_path, monkeypatch, locks):
    monkeypatch.setenv('TEST_AIOS_KEY', 'fixture-key')
    monkeypatch.setenv('TEST_MODEL_KEY', 'fixture-model-key')
    store = ConfigStore(tmp_path / 'config.json')
    store.save(config(tmp_path))
    runner = Runner(store, 'worker')
    bid, rid = str(uuid4()), str(uuid4())
    def handler(request):
        path = request.url.path
        if path.endswith('/protocol'):
            return httpx.Response(200, json={'version': 2})
        if path.endswith('/bind'):
            return httpx.Response(200, json={'binding_id': bid})
        if path.endswith('/submit'):
            return httpx.Response(200, json={'run_id': rid})
        assert path == f'/workspace-client/{bid}/runs/{rid}' and request.method == 'GET'
        return httpx.Response(200, json={'run_id': rid, 'status': 'queued'})
    runner.api.close()
    runner.api = API('http://aios', transport=httpx.MockTransport(handler))
    try:
        with coordination.execution_slot(runner.profile['workspace']) as acquired:
            assert acquired
            assert runner.work('Inspect this project') == {'run_id': rid, 'status': 'queued'}
    finally:
        runner.close()
