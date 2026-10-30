"""Bounded workspace inspection and preflighted, hash-checked file edits."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

EXCLUDED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "client-data",
    ".pytest_cache",
    ".ruff_cache",
}
Text = Annotated[str, Field(max_length=200_000)]


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    expected_hash: str
    operation: Literal["replace", "create", "delete", "move"] = "replace"
    old: Text = ""
    new: Text = ""
    destination: str = ""


class FileTools:
    def __init__(self, root, cancelled):
        self.root, self.cancelled = root, cancelled
        self.lock = threading.RLock()

    def path(self, value="."):
        # Both Windows and POSIX inputs are checked on every host.
        if not isinstance(value, str) or "\\" in value or ":" in value:
            raise ValueError("Use a relative workspace path with forward slashes")
        rel = Path(value)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("Path escapes workspace")
        p = self.root / rel
        if not p.resolve().is_relative_to(self.root):
            raise ValueError("Path escapes workspace")
        cursor = p
        while cursor != self.root:
            if cursor.is_symlink():
                raise ValueError("Symlink paths are not supported by file tools")
            cursor = cursor.parent
        return p

    def files(self, relative="."):
        start = self.path(relative)
        if start.is_file():
            yield start
            return
        count = 0
        for base, dirs, names in os.walk(start, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if d not in EXCLUDED and not Path(base, d).is_symlink()
            )
            for name in sorted(names):
                p = Path(base, name)
                if p.is_symlink() or name == ".env" or name.startswith(".env."):
                    continue
                yield p
                count += 1
                if count >= 5000:
                    return

    def list_files(self, path: str = "."):
        return self.find_files(path=path)

    def find_files(
        self,
        pattern: str = "*",
        path: str = ".",
        limit: Annotated[int, Field(ge=1, le=1000)] = 300,
    ):
        paths = []
        scanned = 0
        for p in self.files(path):
            scanned += 1
            rel = p.relative_to(self.root).as_posix()
            if fnmatch.fnmatchcase(rel, pattern) or (
                pattern.startswith("**/") and fnmatch.fnmatchcase(rel, pattern[3:])
            ):
                paths.append(rel)
                if len(paths) >= limit:
                    return {"files": paths, "truncated": True}
        return {"files": paths, "truncated": scanned >= 5000}

    def search_files(
        self,
        query: Annotated[str, Field(min_length=1, max_length=500)],
        path: str = ".",
        pattern: str = "*",
        case_sensitive: bool = True,
        limit: Annotated[int, Field(ge=1, le=200)] = 40,
    ):
        matches = []
        query = query if case_sensitive else query.casefold()
        scanned, skipped = 0, 0
        for p in self.files(path):
            scanned += 1
            rel = p.relative_to(self.root).as_posix()
            if not fnmatch.fnmatchcase(rel, pattern):
                continue
            try:
                if p.stat().st_size > 1024 * 1024:
                    skipped += 1
                    continue
                for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    if query in (line if case_sensitive else line.casefold()):
                        matches.append({"path": rel, "line": n, "text": line[:300]})
                        if len(matches) >= limit:
                            return {
                                "matches": matches,
                                "truncated": True,
                                "skipped": skipped,
                            }
            except (UnicodeError, OSError):
                skipped += 1
        return {"matches": matches, "truncated": scanned >= 5000, "skipped": skipped}

    def read_file(
        self,
        path: str,
        start: Annotated[int, Field(ge=1)] = 1,
        end: Annotated[int, Field(ge=1)] = 200,
    ):
        p = self.path(path)
        if p.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("File exceeds 2 MiB read limit")
        data = p.read_bytes()
        lines = data.decode("utf-8").splitlines()
        if end < start:
            raise ValueError("Invalid line range")
        end = min(end, start + 199)
        return {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "total_lines": len(lines),
            "lines": [
                {"line": n, "text": lines[n - 1][:1000]}
                for n in range(start, min(end, len(lines)) + 1)
            ],
        }

    def apply_patch(self, path: str, expected_hash: str, old: Text, new: Text):
        operation = "replace" if expected_hash else "create"
        self.apply_edits(
            [
                {
                    "path": path,
                    "expected_hash": expected_hash,
                    "old": old,
                    "new": new,
                    "operation": operation,
                }
            ]
        )
        return {
            "path": path,
            "sha256": hashlib.sha256(self.path(path).read_bytes()).hexdigest(),
            "edited": True,
        }

    def delete_file(self, path: str, expected_hash: str):
        return self.apply_edits(
            [{"path": path, "expected_hash": expected_hash, "operation": "delete"}]
        )

    def move_file(self, path: str, destination: str, expected_hash: str):
        return self.apply_edits(
            [
                {
                    "path": path,
                    "destination": destination,
                    "expected_hash": expected_hash,
                    "operation": "move",
                }
            ]
        )

    def apply_edits(
        self, edits: Annotated[list[Edit], Field(min_length=1, max_length=100)]
    ):
        # Preflight the entire batch before writing. These are recoverable file transactions,
        # not isolation from external commands or a crash-atomic filesystem transaction.
        with self.lock:
            if self.cancelled.is_set():
                raise RuntimeError("Workspace stopped")
            originals, desired, modes = {}, {}, {}
            total = 0
            for raw in edits:
                edit = Edit.model_validate(raw)
                path = self.path(edit.path)
                if ".git" in path.relative_to(self.root).parts:
                    raise ValueError("Use Git tools for repository metadata")
                if path in originals:
                    raise ValueError("Each path may appear only once in a batch")
                exists = path.exists()
                if exists and (
                    not path.is_file() or path.stat().st_size > 2 * 1024 * 1024
                ):
                    raise ValueError("Edits require regular files of at most 2 MiB")
                data = path.read_bytes() if exists else None
                digest = hashlib.sha256(data).hexdigest() if data is not None else ""
                if digest != edit.expected_hash:
                    raise ValueError("File changed; read it again before editing")
                if edit.operation != "create" and not exists:
                    raise ValueError("Edit source does not exist")
                if edit.operation == "create" and (exists or edit.old):
                    raise ValueError(
                        "Create requires an absent path and empty old text"
                    )
                if edit.operation != "move" and edit.destination:
                    raise ValueError("Only move accepts destination")
                originals[path] = data
                modes[path] = path.stat().st_mode & 0o777 if exists else 0o644
                if edit.operation == "create":
                    desired[path] = edit.new.encode("utf-8")
                elif edit.operation == "replace":
                    text = data.decode("utf-8")
                    if not edit.old or text.count(edit.old) != 1:
                        raise ValueError("Old text must match exactly once")
                    desired[path] = text.replace(edit.old, edit.new, 1).encode("utf-8")
                elif edit.operation == "delete":
                    if edit.old or edit.new:
                        raise ValueError("Delete does not accept replacement content")
                    desired[path] = None
                else:
                    if edit.old or edit.new:
                        raise ValueError("Move does not accept replacement content")
                    dest = self.path(edit.destination)
                    if (
                        not edit.destination
                        or dest.exists()
                        or dest in originals
                        or ".git" in dest.relative_to(self.root).parts
                    ):
                        raise ValueError("Move requires a distinct absent destination")
                    originals[dest], desired[dest], modes[dest] = (
                        None,
                        data,
                        modes[path],
                    )
                    desired[path] = None
                total += len(data or b"") + len(desired[path] or b"")
                if total > 4 * 1024 * 1024:
                    raise ValueError("Edit batch exceeds 4 MiB")
            for p, data in originals.items():
                self.path(p.relative_to(self.root).as_posix())
                current = p.read_bytes() if p.exists() else None
                if current != data:
                    raise ValueError("File changed during edit preflight")
            applied = []
            try:
                for p, data in desired.items():
                    if self.cancelled.is_set():
                        raise RuntimeError("Workspace stopped")
                    if data is None:
                        p.unlink()
                    else:
                        self._replace(p, data, modes[p])
                    applied.append(p)
            except (OSError, RuntimeError):
                # Restore files already published; unexpected external writers still require inspection.
                for p in reversed(applied):
                    if originals[p] is None:
                        p.unlink(missing_ok=True)
                    else:
                        self._replace(p, originals[p], modes[p])
                raise
            return {
                "edited": True,
                "files": [
                    {
                        "path": p.relative_to(self.root).as_posix(),
                        "sha256": hashlib.sha256(data).hexdigest()
                        if data is not None
                        else None,
                    }
                    for p, data in desired.items()
                ],
            }

    @staticmethod
    def _replace(path, data, mode):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".aios-edit-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def register(registry, files):
    for name, description in {
        "list_files": "List bounded workspace files, excluding dependencies and secret env files",
        "find_files": "Find files by workspace-relative glob; truncation is explicit",
        "search_files": "Search literal text in UTF-8 files; return paths and line numbers",
        "read_file": "Read bounded lines and obtain the original byte SHA-256",
        "apply_patch": "Replace one exact occurrence with expected hash; empty hash creates a file",
        "apply_edits": "Preflight a batch of hash-checked file creates, replacements, moves or deletions",
        "delete_file": "Delete one file only if its expected hash matches",
        "move_file": "Move a hash-checked file to an absent destination",
    }.items():
        effect = (
            "write"
            if name in {"apply_patch", "apply_edits", "delete_file", "move_file"}
            else "read"
        )
        registry.add(
            name, getattr(files, name), description, "workspace.files", effect=effect
        )
