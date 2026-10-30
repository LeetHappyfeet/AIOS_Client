import io
import json
import sys
import threading

from aios_client.activity import ActivityLog, ProgressLimiter
from aios_client.journal import Journal
from aios_client.workspace import Workspace


def test_live_output_without_model_polling_and_redacted_logs(tmp_path):
    stream = io.StringIO()
    log = ActivityLog(tmp_path / "agent", "worker", ["private-key"], stream)
    output_seen = threading.Event()
    release = tmp_path / "release"
    events = []

    def event(kind, body):
        events.append((kind, body))
        log.process("run-one", kind, body)
        if kind == "process_output" and "visible" in body["output"]:
            output_seen.set()

    root = tmp_path / "workspace"
    root.mkdir()
    workspace = Workspace(root, tmp_path / "artifacts", ["private-key"], on_event=event)
    script = (
        "import sys,time,pathlib; "
        "print('visible private-key \\x1b[31m', end='', flush=True); "
        "print('stderr receipt', file=sys.stderr, flush=True); "
        "p=pathlib.Path(sys.argv[1]); "
        "exec('while not p.exists(): time.sleep(0.01)'); sys.exit(7)"
    )
    try:
        pid = workspace.run_command(argv=[sys.executable, "-u", "-c", script, str(release)])["process_id"]
        assert output_seen.wait(5), "Output must be visible while the command is running"
        assert workspace.processes[pid]["p"].poll() is None
        assert "visible" in stream.getvalue()
        release.touch()
        assert workspace.processes[pid]["done"].wait(5)
        saved = log.path.read_text(encoding="utf-8")
        assert "private-key" not in saved + stream.getvalue()
        assert "[REDACTED]" in saved and "stderr receipt" in saved
        assert "\x1b" not in saved  # Escape terminal control sequences.
        assert "process_started" in saved and '"exit_code": 7' in saved
        assert "run=run-one" in saved and "argv" in saved
        # Reopening the logger appends and does not duplicate handlers.
        log.close()
        log = ActivityLog(tmp_path / "agent", "worker", stream=stream)
        log.write("run-two", "started", "another run")
        assert "run=run-one" in log.path.read_text()
        assert stream.getvalue().count("another run") == 1
    finally:
        release.touch()
        workspace.stop()
        log.close()


def test_progress_dedup_interval_and_run_budget():
    now = [0]
    limiter = ProgressLimiter(lambda: now[0], interval=30, limit=3)
    assert limiter.admit("progress", "accepted", force=True)
    assert not limiter.admit("progress", "accepted", force=True)
    assert not limiter.admit("progress", "too soon")
    now[0] = 30
    assert limiter.admit("progress", "milestone")
    assert not limiter.admit("progress", "milestone")
    assert limiter.admit("blocked", "need file")
    assert not limiter.admit("blocked", "need file")
    assert limiter.admit("blocked", "need key")
    now[0] = 100
    assert not limiter.admit("progress", "over budget")
    assert not limiter.admit("blocked", "over budget")
    assert limiter.admit("progress", "verification ended", force=True)


def test_split_secrets_utf8_and_output_limit(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    events = []
    ready = threading.Event()

    def event(kind, body):
        events.append((kind, body))
        if kind == "process_output" and "ready" in body["output"]:
            ready.set()

    workspace = Workspace(root, tmp_path / "artifacts", ["private-key"], on_event=event)
    workspace.capture_limit = 80
    script = (
        "import sys; sys.stdout.buffer.write(b'ready private-'); sys.stdout.flush(); "
        "sys.stdin.readline(); "
        "sys.stdout.buffer.write(b'key \\xe2'); sys.stdout.flush(); "
        "sys.stdout.buffer.write(b'\\x82\\xac' + b'x'*500); sys.stdout.flush()"
    )
    try:
        pid = workspace.run_command(argv=[sys.executable, "-u", "-c", script], stdin=True)["process_id"]
        assert ready.wait(5)
        workspace.write_process(pid, "\n", close_stdin=True)
        assert workspace.processes[pid]["done"].wait(5)
        output = "".join(body["output"] for kind, body in events if kind == "process_output")
        assert "private-key" not in output and "[REDACTED]" in output
        assert "€" in output and "\ufffd" not in output
        assert len(output.encode()) == 80
        assert sum(kind == "process_output_truncated" for kind, _ in events) == 1
        assert workspace.processes[pid]["log"].read_text() == output
    finally:
        workspace.stop()


def test_logs_cli_reads_saved_tail_without_connecting(tmp_path, monkeypatch, capsys):
    from test_client import config
    from aios_client.cli import main
    from aios_client.config import ConfigStore
    from aios_client.runner import agent_dir

    store = ConfigStore(tmp_path / "client.json")
    store.save(config(tmp_path))
    log = ActivityLog(agent_dir(store, "worker"), "worker", stream=io.StringIO())
    log.write("r", "started", "old entry")
    log.write("r", "finished", "latest entry")
    log.close()
    monkeypatch.setattr(sys, "argv", [
        "aios-client", "--config", str(store.path), "logs", "--agent", "worker", "--lines", "1",
    ])
    main()
    output = capsys.readouterr().out
    assert str(log.path) in output and "latest entry" in output
    assert "old entry" not in output


def test_upgrade_discards_only_pending_raw_tool_observations(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.queue("tool", "r", "runs/r/observe", {"payload": {"tool": "read_file", "result": "raw"}})
    journal.queue("summary", "r", "runs/r/observe", {"text": "summary", "payload": {"verification": "worker_evidence"}})
    journal.queue("finish", "r", "runs/r/finish", {"result": {"report": "done"}})
    try:
        assert journal.discard_tool_observations() == 1
        assert journal.discard_tool_observations() == 0
        sent = []

        class Sink:
            def request(self, method, path, json):
                sent.append((path, json))

        journal.flush(Sink())
        assert [path for path, _ in sent] == ["runs/r/observe", "runs/r/finish"]
        assert "raw" not in json.dumps(sent)
    finally:
        journal.db.close()
