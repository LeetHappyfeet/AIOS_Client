"""Substantive findings reach the transport without extra model calls."""
import json
import threading
import time
from uuid import uuid4

import httpx
import pytest

from aios_client.assignment import assignment
from aios_client.http import API
from aios_client.reporting import result_summary
from aios_client.verification import snapshot, verify
from aios_client.workspace import Workspace
from test_v3 import Service, make_runner, model_api


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    w = Workspace(root, tmp_path / 'artifacts', secrets=['PRIVATE-KEY'])
    yield w
    w.stop()


def report(**extra):
    return dict(outcome='completed', summary='Files created under /home/aiosagent.',
                evidence=['notes/, work/, temp/ exist'], limitations=['Worker observations only'], **extra)


def test_existing_expected_experiment_report_bridges_only_checked_bytes(workspace):
    path = f'aios-experiment-{uuid4()}.json'
    spec = assignment({'objective': 'Create folders', 'expected_outputs': [{'path': path}]})
    baseline = snapshot(workspace)
    (workspace.root / path).write_text(json.dumps(report()))
    verify(workspace, spec, baseline, threading.Event(), time.monotonic() + 10)
    summary = result_summary(workspace, spec, 'Box is built and checked.')
    assert summary['outcome'] == 'completed'
    assert summary['findings'] == ['notes/, work/, temp/ exist']
    assert summary['limitations'] == ['Worker observations only']
    assert summary['source'] == path and len(summary['source_sha256']) == 64
    (workspace.root / path).write_text(json.dumps(report(summary_override='Changed later')))
    assert result_summary(workspace, spec, 'Done') is None


def test_arbitrary_json_and_symlink_reports_are_not_collected(workspace, tmp_path):
    path = f'aios-experiment-{uuid4()}.json'
    outside = tmp_path / 'outside.json'
    outside.write_text(json.dumps(report()))
    (workspace.root / path).write_text(json.dumps(report()))
    spec = assignment({'objective': 'Inspect', 'expected_outputs': [{'path': path}]})
    baseline = snapshot(workspace)
    verify(workspace, spec, baseline, threading.Event(), time.monotonic() + 10)
    (workspace.root / path).unlink()
    (workspace.root / path).symlink_to(outside)
    assert result_summary(workspace, spec, 'Done') is None
    (workspace.root / 'unrelated.json').write_text(json.dumps(report()))
    assert result_summary(workspace, assignment('Inspect'), 'Done') is None


def test_content_preserves_newlines_redacts_and_bounds_encoded_unicode(workspace):
    spec = assignment('Read files')
    value = report(requested_content=[{'label': 'README.md', 'text': '# Title\nPRIVATE-KEY\n'}])
    result = result_summary(workspace, spec, json.dumps(value))
    assert result['requested_content'][0]['text'].startswith('# Title\n')
    assert 'PRIVATE-KEY' not in json.dumps(result)
    assert result['requested_content'][0]['truncated'] is False
    value['requested_content'] = [{'label': str(i), 'text': 'Ω\n' * 20000} for i in range(10)]
    result = result_summary(workspace, spec, json.dumps(value))
    assert len(json.dumps(result)) <= 18000
    assert result['content_sections_omitted'] >= 6
    assert any(s['truncated'] for s in result['requested_content'])


def test_legacy_and_malformed_results_do_not_fabricate_success(workspace):
    for text in ('Done', '[]', '{"outcome":"completed"}'):
        assert result_summary(workspace, assignment('Inspect'), text) is None
    result = result_summary(workspace, assignment('Inspect'),
                            json.dumps(report(requested_content='wrong')))
    assert result['requested_content'] == []


def test_runner_sends_structured_result_in_same_execution_loop(tmp_path, monkeypatch):
    runner = make_runner(tmp_path, monkeypatch)
    service = Service(assignment('Return the README text'))
    runner.api = API('http://aios', transport=httpx.MockTransport(service.handle))
    calls = []
    payload = report(requested_content=[{'label': 'README.md', 'text': '# Box\nActual content\n'}])

    def model(body):
        calls.append(body)
        return {'role': 'assistant', 'content': json.dumps(payload)}

    runner.model = model_api(model)
    try:
        assert runner.work()['status'] == 'finished'
        result = service.bodies('/finish')[0]['result']
        assert result['result_summary']['requested_content'][0]['text'] == '# Box\nActual content\n'
        assert result['result_summary']['outcome'] == 'completed'
        assert result['task_outcome'] == 'not_independently_verified'
        assert len(calls) == 1
        assert len(service.bodies('/observe')) == 2  # No per-tool ingestion.
    finally:
        runner.close()
