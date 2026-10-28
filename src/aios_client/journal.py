from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .http import APIError


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.delivery_lock = threading.Lock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, agent TEXT, status TEXT, task TEXT,
              result TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run TEXT, kind TEXT, body TEXT, time REAL);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, run TEXT, path TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS claims(run TEXT PRIMARY KEY, body TEXT, cursor INTEGER DEFAULT 0);
        """)
        self.db.commit()

    def start(self, run, agent, task, metadata=None):
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT status FROM runs WHERE id=?", (run,)
            ).fetchone()
            if old:
                raise ValueError(
                    "Run already exists locally; inspect it before starting another attempt"
                )
            self.db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?)",
                (run, agent, "running", task, "{}", time.time()),
            )
            if metadata is not None:
                self.db.execute(
                    "INSERT INTO claims(run,body) VALUES(?,?)",
                    (run, json.dumps(metadata)),
                )

    def event(self, run, kind, body):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO events(run,kind,body,time) VALUES(?,?,?,?)",
                (run, kind, json.dumps(body), time.time()),
            )

    def finish(self, run, status, result, report_path=None, report=None):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE runs SET status=?, result=?,updated=? WHERE id=?",
                (status, json.dumps(result), time.time(), run),
            )
            if report_path is not None:
                self.db.execute(
                    "INSERT INTO outbox VALUES(?,?,?,?)",
                    ("finish:" + run, run, report_path, json.dumps(report)),
                )

    def recover(self):
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT r.id,c.body FROM runs r JOIN claims c ON r.id=c.run WHERE r.status='running'"
            ).fetchall()
            for rid, body in rows:
                m = json.loads(body)
                result = {
                    "error": "Client interrupted; inspect uncertain operations before resubmitting",
                    "verification": "unverified",
                }
                # Recovery reports interruption; it never restarts model or shell operations.
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox VALUES(?,?,?,?)",
                    (
                        "finish:" + rid,
                        rid,
                        m["prefix"] + "/finish",
                        json.dumps(
                            {**m["lease"], "status": "interrupted", "result": result}
                        ),
                    ),
                )
            self.db.execute(
                "UPDATE runs SET status='interrupted' WHERE status='running'"
            )

    def queue(self, key, run, path, body):
        with self.lock, self.db:
            old = self.db.execute(
                "SELECT run,path,body FROM outbox WHERE id=?", (key,)
            ).fetchone()
            encoded = json.dumps(body)
            if old and old != (run, path, encoded):
                raise ValueError("Pending report ID reused with changed content")
            self.db.execute(
                "INSERT OR IGNORE INTO outbox VALUES(?,?,?,?)",
                (key, run, path, encoded),
            )

    def flush(self, api):
        with self.delivery_lock:
            self._flush(api)

    def _flush(self, api):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,run,path,body FROM outbox ORDER BY rowid"
            ).fetchall()
        for key, run, path, body in rows:
            try:
                response = api.request("POST", path, json=json.loads(body))
                if (
                    path.endswith("/finish")
                    and isinstance(response, dict)
                    and response.get("status")
                    in {"finished", "failed", "cancelled", "interrupted"}
                    and isinstance(response.get("result"), dict)
                ):
                    self.finish(run, response["status"], response["result"])
            except APIError as exc:
                if exc.status != 409 or not path.endswith(("/observe", "/progress")):
                    raise
                status_path = path.rsplit("/", 1)[0].replace(
                    "workspace-client/v3/", "workspace-client/"
                )
                state = api.request("GET", status_path)
                if not state.get("cancel_requested") and state.get("status") not in {
                    "cancelled",
                    "interrupted",
                    "failed",
                    "finished",
                }:
                    raise
                self.event(
                    run, "report_rejected", {"path": path, "reason": "Run stopped"}
                )
            with self.lock, self.db:
                self.db.execute("DELETE FROM outbox WHERE id=?", (key,))

    def claims(self):
        with self.lock:
            return [
                {"run_id": r[0], **json.loads(r[1]), "cursor": r[2]}
                for r in self.db.execute(
                    "SELECT run,body,cursor FROM claims"
                ).fetchall()
            ]

    def remote_events(self, run, events, cursor):
        with self.lock, self.db:
            previous = self.db.execute(
                "SELECT cursor FROM claims WHERE run=?", (run,)
            ).fetchone()[0]
            if cursor < previous:
                raise ValueError("Remote event cursor moved backwards")
            for event in events:
                if event["sequence"] > previous:
                    self.db.execute(
                        "INSERT INTO events(run,kind,body,time) VALUES(?,?,?,?)",
                        (run, "remote_event", json.dumps(event), time.time()),
                    )
            self.db.execute("UPDATE claims SET cursor=? WHERE run=?", (cursor, run))

    def status(self, agent=None):
        with self.lock:
            sql = "SELECT id,agent,status,task,result FROM runs"
            args = ()
            if agent:
                sql += " WHERE agent=?"
                args = (agent,)
            rows = self.db.execute(
                sql + " ORDER BY updated DESC LIMIT 30", args
            ).fetchall()
            return [
                dict(
                    id=r[0], agent=r[1], status=r[2], task=r[3], result=json.loads(r[4])
                )
                for r in rows
            ]

    def events(self, run):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,kind,body FROM events WHERE run=? ORDER BY id DESC LIMIT 30",
                (run,),
            ).fetchall()
            return [
                {"id": r[0], "kind": r[1], "body": json.loads(r[2])}
                for r in reversed(rows)
            ]
