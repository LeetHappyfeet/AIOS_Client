import io
import subprocess
import sys

import httpx
import pytest

from aios_client.activity import ActivityLog
from aios_client.http import API, APIUnavailable
from aios_client.journal import Journal
from aios_client.workspace import Workspace


def test_plain_workspace_skips_git_once(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    w = Workspace(root, tmp_path / "artifacts")
    try:
        for _ in range(3):
            receipt = w.git_status()
            assert receipt["is_repository"] is False
            assert receipt["status"] == "skipped"
            assert receipt["head"] is None
            assert receipt["output"] == ""
        assert w.git_diff()["status"] == "skipped"
        assert w.git_show()["status"] == "skipped"
        assert len(w.processes) == 1
    finally:
        w.stop()


def test_git_unborn_and_committed_heads(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True
        ).strip()

    git("init")
    w = Workspace(root, tmp_path / "artifacts")
    try:
        receipt = w.git_status()
        assert receipt["is_repository"] is True
        assert receipt["status"] == "available"
        assert receipt["head"] is None
        assert receipt["head_status"] == "unborn"
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "test",
        )
        assert w.git_status()["head"] == git("rev-parse", "HEAD")
    finally:
        w.stop()


def test_git_probe_errors_do_not_claim_repository_absence(tmp_path, monkeypatch):
    w = Workspace(tmp_path, tmp_path / "artifacts")
    try:
        monkeypatch.setattr(
            w.git, "run", lambda args: {"exit_code": 128, "output": "permission denied"}
        )
        receipt = w.git_status()
        assert receipt["is_repository"] is None
        assert receipt["status"] == "failed"
        assert receipt["head"] is None
        monkeypatch.setattr(
            w.git, "run", lambda args: {"exit_code": 0, "output": "true\n"}
        )
        receipt = w.git_status()
        assert receipt["is_repository"] is True
        assert receipt["head"] is None  # Non-SHA output must not become a HEAD.
        assert receipt["head_status"] == "failed"
    finally:
        w.stop()


def test_activity_newlines_and_tool_receipts(tmp_path):
    stream = io.StringIO()
    log = ActivityLog(tmp_path, "worker", ["private-key"], stream)
    try:
        log.process("r", "process_output", {"process_id": "p", "output": "one\n"})
        log.process(
            "r", "process_output", {"process_id": "p", "output": "two\n\nthree\n"}
        )
        output = stream.getvalue().splitlines()
        assert (
            len(output) == 4
        )  # Keep the real blank line, discard artificial trailing records.
        log.tool_result(
            "r",
            "apply_patch",
            "call",
            {"ok": True, "edited": True, "path": "a.py", "sha256": "abc"},
        )
        log.tool_result(
            "r",
            "read_artifact",
            "read",
            {"artifact_id": "a.log", "complete": True, "output": "PRIVATE_CONTENT"},
        )
        log.tool_result("r", "broken", "bad", {"error": "private-key"})
        saved = log.path.read_text()
        assert '"path": "a.py"' in saved and '"edited": true' in saved
        assert '"artifact_id": "a.log"' in saved
        assert "PRIVATE_CONTENT" not in saved and "private-key" not in saved
        assert "[REDACTED]" in saved
    finally:
        log.close()


def test_process_start_logs_resolved_cwd(tmp_path):
    events = []
    w = Workspace(
        tmp_path, tmp_path / "artifacts", on_event=lambda *event: events.append(event)
    )
    try:
        pid = w.run_command(argv=[sys.executable, "-c", "pass"])["process_id"]
        assert w.processes[pid]["done"].wait(5)
        started = next(body for kind, body in events if kind == "process_started")
        assert started["resolved_cwd"] == str(tmp_path.resolve())
        assert w.process_status()["processes"][0]["resolved_cwd"] == str(
            tmp_path.resolve()
        )
    finally:
        w.stop()


def test_delivery_retry_counts_survive_restart_and_match_wire_bytes(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path)
    journal.queue("stable", "r", "runs/r/observe", {"text": "Résumé €"})
    received = []

    def offline(request):
        received.append(request.content)
        raise httpx.ConnectError("offline")

    api = API("http://aios", transport=httpx.MockTransport(offline))
    try:
        with pytest.raises(APIUnavailable):
            journal.flush(api)
        summary = journal.delivery_summary("r")
        assert summary["pending"] == 1 and summary["failed_attempts"] == 1
        assert summary["attempted_payload_bytes"] == len(received[0])
    finally:
        api.close()
        journal.db.close()
    journal = Journal(path)
    api = API(
        "http://aios",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})),
    )
    try:
        journal.flush(api)
        journal.flush(api)
        summary = journal.delivery_summary("r")
        assert summary["reports"] == 1 and summary["acknowledged"] == 1
        assert summary["attempts"] == 2 and summary["pending"] == 0
        assert summary["acknowledged_payload_bytes"] == len(received[0])
        assert summary["by_kind"] == {"observe": 1}
    finally:
        api.close()
        journal.db.close()


def test_terminal_delivery_rejection_is_not_acknowledged(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.queue("stable", "r", "runs/r/progress", {"summary": "done"})

    def handle(request):
        return (
            httpx.Response(409, json={})
            if request.method == "POST"
            else httpx.Response(200, json={"status": "cancelled"})
        )

    api = API("http://aios", transport=httpx.MockTransport(handle))
    try:
        journal.flush(api)
        summary = journal.delivery_summary("r")
        assert summary["rejected"] == 1 and summary["acknowledged"] == 0
        assert summary["pending"] == 0 and summary["failed_attempts"] == 1
    finally:
        api.close()
        journal.db.close()


def test_upgrade_tracks_preexisting_outbox_without_replaying_tools(tmp_path):
    path = tmp_path / "journal.sqlite3"
    journal = Journal(path)
    journal.queue("raw", "r", "runs/r/observe", {"payload": {"tool": "read_file"}})
    journal.queue("keep", "r", "runs/r/finish", {"result": {"report": "done"}})
    # Simulate the previous schema: pending notifications exist, audit table does not.
    journal.db.execute("DROP TABLE deliveries")
    journal.db.commit()
    journal.db.close()
    journal = Journal(path)
    sent = []

    def handle(request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    api = API("http://aios", transport=httpx.MockTransport(handle))
    try:
        assert journal.delivery_summary("r")["pending"] == 2
        assert journal.discard_tool_observations() == 1
        journal.flush(api)
        assert sent == ["/runs/r/finish"]
        summary = journal.delivery_summary("r")
        assert summary["discarded"] == 1 and summary["acknowledged"] == 1
        assert summary["attempts"] == 1 and summary["pending"] == 0
    finally:
        api.close()
        journal.db.close()
