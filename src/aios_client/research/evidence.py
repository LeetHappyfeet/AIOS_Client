"""Bounded, per-agent research receipts; not character memories.

Evidence persisted here is retrieval provenance, never an assertion of truth.
The model can only cite results read during the current authorized work run.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class EvidenceStore:
    def __init__(self, path, run_id):
        self.path = Path(path) if path is not None else None
        self.run_id = str(run_id) if run_id is not None else None

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("Research evidence store must not be a symlink")
        if not self.path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                fd = os.open(self.path, flags, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        if self.path.is_symlink():
            raise ValueError("Research evidence store must not be a symlink")
        db = sqlite3.connect(self.path, timeout=4)
        db.execute("CREATE TABLE IF NOT EXISTS evidence ("
                   "run_id TEXT NOT NULL, evidence_id TEXT NOT NULL,"
                   "record TEXT NOT NULL, verified INTEGER NOT NULL,"
                   "retrieved_at TEXT NOT NULL,"
                   "PRIMARY KEY(run_id, evidence_id))")
        return db

    def save(self, item, *, verified=False, section=None, document_sha256=None):
        if self.path is None or self.run_id is None:
            return
        # Never persist full page contents or extraction buffers. Records are
        # sufficient for citation and audit, not a second searchable corpus.
        record = {
            "evidence_id": item["evidence_id"],
            "source_id": item["source_id"],
            "kind": item["kind"],
            "title": item["title"][:200],
            "location": item.get("location"),
            "content_scope": "retrieved_passage" if verified else "discovery_excerpt",
            "document_sha256": document_sha256 or item.get("document_sha256"),
            "excerpt": item.get("excerpt", "")[:1500],
            "section": section if verified else None,
            "public_url": item.get("url") if item["kind"] == "web_search_result" else None,
        }
        with self._open() as db:
            db.execute("INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?)", (
                self.run_id, item["evidence_id"],
                json.dumps(record, ensure_ascii=True), int(verified),
                datetime.now(timezone.utc).isoformat()))
            # A bounded historical audit cache. No journal/HUD replay occurs.
            db.execute("DELETE FROM evidence WHERE rowid IN ("
                       "SELECT rowid FROM evidence ORDER BY retrieved_at DESC"
                       " LIMIT -1 OFFSET 5000)")
            db.commit()

    def get(self, evidence_id):
        if self.path is None or self.run_id is None or not self.path.exists():
            return None
        with self._open() as db:
            row = db.execute("SELECT record,verified,retrieved_at FROM evidence"
                             " WHERE run_id=? AND evidence_id=?",
                             (self.run_id, evidence_id)).fetchone()
        if row is None:
            return None
        result = json.loads(row[0])
        result["read_verified"] = bool(row[1])
        result["retrieved_at"] = row[2]
        return result
