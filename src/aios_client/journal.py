from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, agent TEXT, status TEXT, task TEXT,
              result TEXT, updated REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run TEXT, kind TEXT, body TEXT, time REAL);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, run TEXT, path TEXT, body TEXT);
        """)
        self.db.commit()

    def start(self, run, agent, task):
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

    def event(self, run, kind, body):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO events(run,kind,body,time) VALUES(?,?,?,?)",
                (run, kind, json.dumps(body), time.time()),
            )

    def finish(self, run, status, result):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE runs SET status=?, result=?,updated=? WHERE id=?",
                (status, json.dumps(result), time.time(), run),
            )

    def recover(self):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE runs SET status='interrupted' WHERE status='running'"
            )

    def queue(self, key, run, path, body):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO outbox VALUES(?,?,?,?)",
                (key, run, path, json.dumps(body)),
            )

    def flush(self, api):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,path,body FROM outbox ORDER BY rowid"
            ).fetchall()
        for key, path, body in rows:
            api.request("POST", path, json=json.loads(body))
            with self.lock, self.db:
                self.db.execute("DELETE FROM outbox WHERE id=?", (key,))

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
