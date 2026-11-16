"""Optional incremental SQLite FTS5 document cache, separate from character memory."""
import os
import sqlite3
from pathlib import Path
from .documents import MAX_DOCUMENT, digest, extract_document

EXTENSIONS = {".txt", ".md", ".rst", ".csv", ".json", ".html", ".htm", ".pdf", ".epub"}

class DocumentIndex:
    def __init__(self, path):
        self.path = Path(path)

    def search(self, source, query, limit, guard):
        root = Path(source.root)
        if root.is_symlink() or not root.is_dir():
            return {"error": "local_root_unavailable"}
        root = root.resolve(strict=True)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with sqlite3.connect(self.path, timeout=4) as db:
                db.execute("CREATE TABLE IF NOT EXISTS docs (source TEXT, path TEXT,"
                           " root TEXT, mtime INTEGER, size INTEGER, hash TEXT,"
                           " PRIMARY KEY(source,path))")
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5("
                           "source UNINDEXED, path UNINDEXED, section UNINDEXED, text)")
                # A source ID remapped to another directory invalidates its cache.
                existing = db.execute("SELECT DISTINCT root FROM docs WHERE source=?",
                                      (source.id,)).fetchall()
                if existing and any(row[0] != str(root) for row in existing):
                    db.execute("DELETE FROM passages WHERE source=?", (source.id,))
                    db.execute("DELETE FROM docs WHERE source=?", (source.id,))
                previous = {p: (m, sz) for p,m,sz in db.execute(
                    "SELECT path,mtime,size FROM docs WHERE source=?", (source.id,))}
                seen, scanned = set(), 0
                for base, dirs, files in os.walk(root, followlinks=False):
                    guard()
                    dirs[:] = sorted(d for d in dirs if not (Path(base)/d).is_symlink())
                    for name in sorted(files):
                        path = Path(base) / name
                        if path.is_symlink() or path.suffix.lower() not in EXTENSIONS:
                            continue
                        scanned += 1
                        if scanned > 1000:
                            break
                        try:
                            if not path.resolve().is_relative_to(root):
                                continue
                            stat = path.stat()
                            rel = path.relative_to(root).as_posix()
                            seen.add(rel)
                            if stat.st_size > MAX_DOCUMENT:
                                continue
                            if previous.get(rel) == (stat.st_mtime_ns, stat.st_size):
                                continue
                            with path.open("rb") as f:
                                raw = f.read(MAX_DOCUMENT + 1)
                            if len(raw) > MAX_DOCUMENT:
                                continue
                            sections, _ = extract_document(raw, path.suffix.lower())
                            db.execute("DELETE FROM passages WHERE source=? AND path=?",
                                       (source.id, rel))
                            db.executemany(
                                "INSERT INTO passages(source,path,section,text) VALUES(?,?,?,?)",
                                ((source.id, rel, sec, txt) for sec, txt in sections))
                            db.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?)",
                                       (source.id, rel, str(root), stat.st_mtime_ns,
                                        stat.st_size, digest(raw)))
                        except (OSError, ValueError):
                            continue
                    if scanned > 1000:
                        break
                if scanned <= 1000:
                    for missing in previous.keys() - seen:
                        db.execute("DELETE FROM passages WHERE source=? AND path=?",
                                   (source.id, missing))
                        db.execute("DELETE FROM docs WHERE source=? AND path=?",
                                   (source.id, missing))
                db.commit()
                terms = [t for t in query.split() if t.strip()]
                if not terms:
                    return {"matches": []}
                match = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms[:16])
                matches = db.execute(
                    "SELECT p.path,p.section,p.text,d.hash FROM passages p"
                    " JOIN docs d ON p.source=d.source AND p.path=d.path"
                    " WHERE passages MATCH ? AND p.source=? LIMIT ?",
                    (match, source.id, limit)).fetchall()
                return {"matches": matches, "scan_truncated": scanned > 1000}
        except (sqlite3.Error, OSError):
            return {"error": "index_unavailable"}
