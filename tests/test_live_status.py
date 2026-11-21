import json
import sys
import threading

import httpx
from fastapi.testclient import TestClient

from aios_client import cli
from aios_client.activity import ActivityLog
from aios_client.assignment import assignment
from aios_client.http import API
from aios_client.live import LiveStatus, read_live
from aios_client.server import create_app
from test_v3 import Service, make_runner, model_api


def test_worker_status_is_visible_to_other_entry_points(tmp_path, monkeypatch, capsys):
    runner = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Inspect worker-secret"))
    runner.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    in_model = threading.Event()
    release_model = threading.Event()
    errors = []

    def model(_body):
        in_model.set()
        assert release_model.wait(10)
        return {"role": "assistant", "content": "Done"}

    runner.model = model_api(model)

    def work():
        try:
            runner.work()
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert in_model.wait(5)
        with TestClient(create_app(runner.store), base_url="http://127.0.0.1:8765") as browser:
            row, = browser.get("/api/status").json()
        assert row["active"]
        assert row["live"]["phase"] == "waiting_model_response"
        assert row["live"]["run_id"] == service.rid
        assert row["live"]["objective"] == "Inspect [REDACTED]"
        assert row["last_run"] is None

        args = ["aios-client", "--config", str(runner.store.path), "status", "--agent", "worker"]
        monkeypatch.setattr(sys, "argv", [*args, "--live"])
        cli.main()
        assert json.loads(capsys.readouterr().out)["live"]["phase"] == "waiting_model_response"

        release_model.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        monkeypatch.setattr(sys, "argv", [*args, "--live"])
        cli.main()
        status = json.loads(capsys.readouterr().out)
        assert status["live"]["phase"] == "stopped"
        assert not status["live"]["active"]
        assert status["last_run"]["execution"] == "finished"
        assert status["last_run"]["verification"] == "not_configured"
        assert status["last_run"]["delivery"] == "acknowledged"

        monkeypatch.setattr(sys, "argv", args)
        cli.main()
        assert json.loads(capsys.readouterr().out)[0]["id"] == service.rid
    finally:
        release_model.set()
        thread.join(timeout=10)
        runner.close()


def test_stale_snapshot_does_not_report_a_running_worker(tmp_path):
    (tmp_path / "live.json").write_text(json.dumps({
        "active": True, "updated_at": 1, "phase_since": 1,
    }))
    assert read_live(tmp_path) is None


def test_concise_console_retains_raw_activity_and_names_shutdown_wait(tmp_path, capsys):
    activity = ActivityLog(tmp_path, "worker", ["private-key"], console=False)
    status = LiveStatus(tmp_path, "worker", activity.operator_text, concise=True)
    try:
        with status.track():
            activity.write("run", "process_output", "full output private-key")
            status.set_phase("waiting_model_response", "Step 2")
            status.stop()
            status.set_phase("listening")
            assert read_live(tmp_path)["phase"] == "stopping"
        output = capsys.readouterr().err
        assert "Stopping · Waiting for model response · Step 2" in output
        assert "Worker stopped" in output
        assert "full output" not in output
        assert "full output [REDACTED]" in activity.path.read_text()
    finally:
        activity.close()
