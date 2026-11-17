"""Operator-imported Calibre PDFs, with bounded extraction and page coverage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path

MAX_STAGE_PAGES = 2000
MAX_PAGE_CHARS = 8000
MAX_INDEX_CHARS = 2_000_000


def _index_path(root, source, catalog_id):
    if source.adapter != "calibre_opds" or not source.enabled:
        raise ValueError("An enabled Calibre source is required")
    if not catalog_id.strip() or len(catalog_id) > 300:
        raise ValueError("Use the exact Calibre catalog ID (1–300 characters)")
    key = json.dumps([source.id, source.endpoint, catalog_id, "pdf"])
    return root / (hashlib.sha256(key.encode()).hexdigest() + ".sqlite3")


def _poppler(command, deadline, *, page_timeout):
    # Redirect output to a regular file so RLIMIT_FSIZE also bounds parser output.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    with tempfile.TemporaryFile() as output:
        try:
            proc = subprocess.run(
                ["prlimit", "--as=805306368", "--fsize=1048576", "--", *command],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                env={**os.environ, "LC_ALL": "C"}, timeout=min(page_timeout, remaining),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode:
            return None
        output.seek(0)
        return output.read().decode("utf-8", errors="replace")


def stage_pdf(root, source, catalog_id, pdf, *, max_bytes, max_pages, timeout):
    """Publish a complete index transaction, including any partial coverage."""
    if not 1 <= max_pages <= MAX_STAGE_PAGES or not 1 <= timeout <= 1800:
        raise ValueError("Staging allows 1–2000 pages and a timeout of 1–1800 seconds")
    if not 1 <= max_bytes <= 512 * 1024 * 1024:
        raise ValueError("Staging byte limit must be between 1 byte and 512 MiB")
    target = _index_path(root, source, catalog_id)
    for command in ("prlimit", "pdfinfo", "pdftotext"):
        if shutil.which(command) is None:
            raise ValueError("PDF staging requires Poppler utilities and prlimit")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=root) as temporary:
        snapshot = Path(temporary) / "document.pdf"
        checksum = hashlib.sha256()
        fd = os.open(pdf, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle, snapshot.open("wb") as output:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise ValueError("PDF must be a regular file within the staging byte limit")
            size = 0
            while block := handle.read(1024 * 1024):
                size += len(block)
                if size > max_bytes:
                    raise ValueError("PDF exceeds the staging byte limit")
                checksum.update(block)
                output.write(block)
        deadline = time.monotonic() + timeout
        info = _poppler(["pdfinfo", str(snapshot)], deadline, page_timeout=15)
        page_count = re.search(r"^Pages:\s*(\d+)\s*$", info or "", re.MULTILINE)
        if not page_count or int(page_count[1]) < 1:
            raise ValueError("Could not read PDF page count")
        total = int(page_count[1])
        coverage = dict(total_pages=total, attempted_pages=0, indexed_pages=0,
                        no_text_pages=0, failed_pages=0, clipped_pages=0,
                        unprocessed_pages=0)
        database = Path(temporary) / "index.sqlite3"
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("CREATE TABLE document (sha256 TEXT, coverage TEXT)")
            db.execute("CREATE TABLE pages (number INTEGER PRIMARY KEY, text TEXT)")
            remaining = MAX_INDEX_CHARS
            for page in range(1, min(max_pages, total) + 1):
                if remaining <= 0 or time.monotonic() >= deadline:
                    break
                text = _poppler(["pdftotext", "-f", str(page), "-l", str(page),
                                 "-enc", "UTF-8", str(snapshot), "-"], deadline,
                                page_timeout=10)
                coverage["attempted_pages"] += 1
                if text is None:
                    coverage["failed_pages"] += 1
                    continue
                text = text.strip()
                if not text:
                    coverage["no_text_pages"] += 1
                    continue
                cap = min(remaining, MAX_PAGE_CHARS)
                coverage["clipped_pages"] += int(len(text) > cap)
                db.execute("INSERT INTO pages VALUES (?, ?)", (page, text[:cap]))
                remaining -= min(cap, len(text))
                coverage["indexed_pages"] += 1
            attempted = coverage["attempted_pages"]
            coverage["unprocessed_pages"] = total - attempted
            if total > attempted:
                coverage["unprocessed_range"] = f"{attempted + 1}–{total}"
            db.execute("INSERT INTO document VALUES (?, ?)",
                       (checksum.hexdigest(), json.dumps(coverage)))
        os.replace(database, target)
    return {"source_id": source.id, "catalog_id": catalog_id,
            "document_sha256": checksum.hexdigest(), "coverage": coverage}


def unstage_pdf(root, source, catalog_id):
    _index_path(root, source, catalog_id).unlink(missing_ok=True)


def load_staged_pdf(root, source, catalog_id, guard):
    """Read one bounded snapshot. Its physical page numbers survive blank pages."""
    if root is None or not catalog_id or len(catalog_id) > 300:
        return None
    path = _index_path(root, source, catalog_id)
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            sha256, raw = db.execute("SELECT sha256, coverage FROM document").fetchone()
            coverage = json.loads(raw)
            sections = {}
            for number, text in db.execute("SELECT number, text FROM pages ORDER BY number"):
                guard()
                sections[number - 1] = (f"page:{number}", text)
    except (sqlite3.Error, ValueError, TypeError):
        return {"error": "staged_pdf_index_unavailable"}
    return {"sections": sections, "sha256": sha256, "coverage": coverage,
            "limitations": [key for key in ("no_text_pages", "failed_pages",
                                             "clipped_pages", "unprocessed_pages")
                            if coverage[key]],
            "error": None if sections else "no_text"}
