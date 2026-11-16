"""Regression coverage: stale AIOS progress cannot prevent Listen startup.

The server's normal claim path transitions expired running runs to interrupted.
The client must retain (not discard) its outbox while waiting for that state.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from aios_client.http import APIError, APIUnavailable
from aios_client.journal import Journal, _expired_unfinished_run


RUN = "1bdcf6e8-5dc9-40da-8536-6b017e5f844e"
BINDING = "afc20de3-6d20-4a7f-a069-5718b4cbeda0"
ROOT = f"workspace-client/{BINDING}/runs/{RUN}"
PROGRESS = f"workspace-client/v3/{BINDING}/runs/{RUN}/progress"
FINISH = ROOT + "/finish"


def expiry(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class FakeAIOS:
    def __init__(self, *, remote_status="running", remaining=-600):
        self.remote_status = remote_status
        self.lease_expiration = expiry(remaining)
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method == "GET" and path == ROOT:
            return {"status": self.remote_status,
                    "lease_expires_at": self.lease_expiration,
                    "cancel_requested": False}
        if method == "POST" and path in {PROGRESS, FINISH}:
            raise APIError(409, "POST", path)
        raise AssertionError((method, path))


def prepared(tmp_path):
    journal = Journal(tmp_path / "journal.sqlite3")
    journal.start(RUN, "Renamon", "Calibre research")
    report = {"status": "failed", "result": {"error": "Lease renewal rejected"}}
    journal.queue("stale-progress", RUN, PROGRESS,
                  {"event_key": "stale-progress", "summary": "Work failed"})
    journal.finish(RUN, "failed", report["result"],
                   report_path=FINISH, report=report)
    return journal


def pending(journal):
    return journal.db.execute(
        "SELECT id FROM outbox ORDER BY rowid"
    ).fetchall()


def test_expired_nonterminal_progress_and_finish_are_deferred_not_lost(tmp_path):
    journal = prepared(tmp_path)
    api = FakeAIOS()
    try:
        journal.flush(api)  # Does not raise even though both POSTs return 409.
        assert pending(journal) == [("stale-progress",), (f"finish:{RUN}",)]
        states = journal.db.execute(
            "SELECT id,state,attempts,failures FROM deliveries ORDER BY rowid"
        ).fetchall()
        assert states == [
            ("stale-progress", "pending", 1, 1),
            (f"finish:{RUN}", "pending", 1, 1),
        ]
        assert journal.db.execute(
            "SELECT status FROM runs WHERE id=?", (RUN,)
        ).fetchone() == ("failed",)
        assert len([call for call in api.calls if call[0] == "POST"]) == 2
        assert len(journal.db.execute(
            "SELECT kind FROM events WHERE run=? AND kind='delivery_deferred'",
            (RUN,),
        ).fetchall()) == 2

        # The next AIOS claim has interrupted the expired run. Previously queued
        # notifications can now be rejected with a durable audit trail.
        api.remote_status = "interrupted"
        journal.flush(api)
        assert pending(journal) == []
        states = journal.db.execute(
            "SELECT state FROM deliveries ORDER BY rowid"
        ).fetchall()
        assert states == [("rejected",), ("rejected",)]
        assert journal.db.execute(
            "SELECT status FROM runs WHERE id=?", (RUN,)
        ).fetchone() == ("failed",)
    finally:
        journal.db.close()


@pytest.mark.parametrize("remaining", [300, 10, -10])
def test_live_or_just_expired_lease_still_blocks_reporting(tmp_path, remaining):
    journal = prepared(tmp_path)
    try:
        with pytest.raises(APIError) as exc:
            journal.flush(FakeAIOS(remaining=remaining))
        assert exc.value.status == 409
        assert len(pending(journal)) == 2
    finally:
        journal.db.close()


@pytest.mark.parametrize("data", [
    {"status": "running", "lease_expires_at": "invalid"},
    {"status": "running"},
    {"status": "queued", "lease_expires_at": expiry(-1000)},
    {"status": "running", "lease_expires_at": "2026-01-01T00:00:00"},
    {},
    None,
])
def test_ambiguous_remote_state_never_authorizes_deferral(data):
    assert not _expired_unfinished_run(data)


def test_running_with_old_aware_timestamp_can_defer():
    assert _expired_unfinished_run({
        "status": "running", "lease_expires_at": expiry(-120),
    })


def test_remote_status_lookup_failure_is_not_discarded(tmp_path):
    journal = prepared(tmp_path)
    class Unreachable(FakeAIOS):
        def request(self, method, path, **kwargs):
            if method == "GET":
                raise APIUnavailable("server unreachable")
            return super().request(method, path, **kwargs)
    try:
        with pytest.raises(APIUnavailable):
            journal.flush(Unreachable())
        assert len(pending(journal)) == 2
    finally:
        journal.db.close()
