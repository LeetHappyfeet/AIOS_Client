import json
import sys
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from aios_client.assignment import assignment, verification_status
from aios_client.verification import snapshot, verify
from aios_client.workspace import Workspace


def test_assignment_rejects_unknown_fields_paths_and_unpaired_goal():
    for value in (
        {"objective": "x", "typo": 1},
        {"objective": "x", "preserve_paths": ["../x"]},
        {"objective": "x", "goal_id": "00000000-0000-0000-0000-000000000001"},
        {
            "objective": "x",
            "required_checks": [
                {"id": "a", "command": "x"},
                {"id": "a", "command": "y"},
            ],
        },
    ):
        with pytest.raises(ValidationError):
            assignment(value)


def setup(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    w = Workspace(root, tmp_path / "artifacts")
    return w, snapshot(w)


def run(w, baseline, **fields):
    spec = assignment({"objective": "do work", **fields})
    try:
        return verify(w, spec, baseline, threading.Event(), time.monotonic() + 10)
    finally:
        w.stop()


def command(code):
    # Portable Python command for both CI operating systems.
    return f'{sys.executable} -c "{code}"'


def test_prose_without_required_checks_cannot_pass(tmp_path):
    w, before = setup(tmp_path)
    evidence = run(w, before)
    assert evidence["status"] == "not_configured"
    assert (
        verification_status(assignment("do work"), {"status": "passed"}) == "incomplete"
    )


def test_new_deleted_and_modified_files_are_in_manifest(tmp_path):
    w, _ = setup(tmp_path)
    (w.root / "old.txt").write_text("old")
    (w.root / "edit.txt").write_text("before")
    before = snapshot(w)
    (w.root / "old.txt").unlink()
    (w.root / "edit.txt").write_text("after")
    (w.root / "new.txt").write_text("new")
    result = run(
        w,
        before,
        required_checks=[{"id": "check", "command": command("print('ok')")}],
        expected_outputs=[{"path": "new.txt"}],
    )
    assert result["status"] == "passed"
    manifest = json.loads((w.artifacts / result["manifest_artifact"]).read_text())
    assert {c["path"] for c in manifest["changes"]} == {
        "old.txt",
        "new.txt",
        "edit.txt",
    }
    assert manifest["final"]["entries"]["new.txt"]["sha256"]
    assert result["checks"][0]["log_sha256"]
    assert result["snapshot_scope"]["root"] == str(w.root)
    assert result["snapshot_scope"]["outside_workspace_observed"] is False


@pytest.mark.parametrize(
    "code,expected",
    [
        ("raise SystemExit(2)", "failed"),
        ("from pathlib import Path;Path('late.txt').write_text('x')", "stale"),
    ],
)
def test_failed_checks_and_checks_that_modify_files(tmp_path, code, expected):
    w, before = setup(tmp_path)
    result = run(w, before, required_checks=[{"id": "check", "command": command(code)}])
    assert result["status"] == expected


def test_preserved_paths_and_missing_outputs(tmp_path):
    w, before = setup(tmp_path)
    (w.root / "protected.txt").write_text("bad")
    result = run(
        w,
        before,
        preserve_paths=["protected.txt"],
        expected_outputs=[{"path": "missing.txt"}],
    )
    assert result["status"] == "failed" and result["violations"] == ["protected.txt"]


def test_check_timeout_is_recorded_even_if_exit_code_could_look_successful(tmp_path):
    w, before = setup(tmp_path)
    result = run(
        w,
        before,
        required_checks=[
            {
                "id": "check",
                "command": command("import time;time.sleep(20)"),
                "timeout_seconds": 1,
            }
        ],
    )
    assert result["status"] == "failed"
    assert result["checks"][0]["timed_out"] is True


def test_manifest_budget_never_silently_claims_completeness(tmp_path, monkeypatch):
    w, before = setup(tmp_path)

    def fail(*a, **kw):
        raise OSError("unreadable")

    (w.root / "x").write_text("x")
    monkeypatch.setattr(Path, "open", fail)
    assert snapshot(w)["complete"] is False
