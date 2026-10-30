"""Bounded workspace inspection and preflighted, hash-checked file edits."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tempfile
import threading
import time
from collections import OrderedDict
from itertools import islice
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import regex
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


class ReadRange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    start: Annotated[int, Field(ge=1)] = 1
    end: Annotated[int, Field(ge=1)] = 200


class FileTools:
    def __init__(self, root, cancelled):
        self.root, self.cancelled = root, cancelled
        self.lock = threading.RLock()
        self.cursors = OrderedDict()

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
        for base, dirs, names in os.walk(start, followlinks=False):
            self._check_cancelled()
            dirs[:] = sorted(
                d for d in dirs if d not in EXCLUDED and not Path(base, d).is_symlink()
            )
            for name in sorted(names):
                self._check_cancelled()
                p = Path(base, name)
                if p.is_symlink() or name == ".env" or name.startswith(".env."):
                    continue
                yield p

    @staticmethod
    def _matches_path(path, pattern):
        return fnmatch.fnmatchcase(path, pattern) or (
            pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
        )

    def _position(self, cursor, options):
        if cursor is None:
            return 0, 1, None
        state = self.cursors.get(cursor)
        if state is None or state[0] != options:
            raise ValueError(
                "Cursor is expired or belongs to different search options; restart the search"
            )
        self.cursors.move_to_end(cursor)
        return state[1:]

    def _continuation(self, options, offset, line=1, digest=None):
        token = str(uuid4())
        self.cursors[token] = (options, offset, line, digest)
        if len(self.cursors) > 128:
            self.cursors.popitem(last=False)
        return token

    def _check_cancelled(self):
        if self.cancelled.is_set():
            raise RuntimeError("Workspace stopped")

    def list_files(self, path: str = ".", cursor: str | None = None):
        return self.find_files(path=path, cursor=cursor)

    def find_files(
        self,
        pattern: str = "*",
        path: str = ".",
        limit: Annotated[int, Field(ge=1, le=1000)] = 300,
        cursor: str | None = None,
        scan_limit: Annotated[int, Field(ge=1, le=5000)] = 5000,
    ):
        options = ("find", path, pattern)
        offset, _, _ = self._position(cursor, options)
        paths, scanned = [], 0
        iterator = islice(self.files(path), offset, None)
        for index, p in enumerate(iterator, offset):
            self._check_cancelled()
            scanned += 1
            rel = p.relative_to(self.root).as_posix()
            if self._matches_path(rel, pattern):
                paths.append(rel)
            if len(paths) >= limit or scanned >= scan_limit:
                return {
                    "files": paths,
                    "truncated": True,
                    "scanned": scanned,
                    "next_cursor": self._continuation(options, index + 1),
                }
        return {
            "files": paths,
            "truncated": False,
            "scanned": scanned,
            "next_cursor": None,
        }

    def search_files(
        self,
        query: Annotated[str, Field(min_length=1, max_length=500)]
        | Annotated[
            list[Annotated[str, Field(min_length=1, max_length=500)]],
            Field(min_length=1, max_length=16),
        ],
        path: str = ".",
        pattern: str = "*",
        case_sensitive: bool = True,
        limit: Annotated[int, Field(ge=1, le=200)] = 40,
        regex_mode: bool = False,
        whole_word: bool = False,
        context_lines: Annotated[int, Field(ge=0, le=5)] = 0,
        mode: Literal["matches", "files", "count"] = "matches",
        cursor: str | None = None,
        scan_limit: Annotated[int, Field(ge=1, le=5000)] = 5000,
    ):
        queries = [query] if isinstance(query, str) else query
        options = (
            "search",
            tuple(queries),
            path,
            pattern,
            case_sensitive,
            regex_mode,
            whole_word,
            context_lines,
            mode,
        )
        offset, first_line, expected_hash = self._position(cursor, options)
        expressions = [q if regex_mode else regex.escape(q) for q in queries]
        expression = "(?:" + "|".join(expressions) + ")"
        if whole_word:
            expression = r"(?<!\w)" + expression + r"(?!\w)"
        try:
            matcher = regex.compile(
                expression, 0 if case_sensitive else regex.IGNORECASE
            )
        except regex.error:
            raise ValueError("Invalid search regular expression") from None
        key = {"matches": "matches", "files": "files", "count": "counts"}[mode]
        results, scanned, skipped, total = [], 0, 0, 0
        deadline = time.monotonic() + 2

        def finish(next_cursor=None, reason=None):
            return {
                key: results,
                "truncated": next_cursor is not None,
                "next_cursor": next_cursor,
                "scanned": scanned,
                "skipped": skipped,
                "total_matches": total,
                "count_scope": "matching_files_in_this_page"
                if mode == "files"
                else "matching_lines_in_this_page",
                "limit_reason": reason,
            }

        iterator = islice(self.files(path), offset, None)
        for index, p in enumerate(iterator, offset):
            self._check_cancelled()
            # End a page before consuming the next file. The cursor always advances
            # even if a page has no matches, so repositories larger than 5000 files work.
            if scanned >= scan_limit or (scanned and time.monotonic() >= deadline):
                return finish(self._continuation(options, index), "scan_budget")
            scanned += 1
            rel = p.relative_to(self.root).as_posix()
            if not self._matches_path(rel, pattern):
                continue
            try:
                if p.stat().st_size > 1024 * 1024:
                    skipped += 1
                    continue
                data = p.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if index == offset and expected_hash and digest != expected_hash:
                    raise ValueError(
                        "Search continuation file changed; restart the search"
                    )
                lines = data.decode("utf-8").splitlines()
            except (UnicodeError, OSError):
                skipped += 1
                continue
            count = 0
            start = first_line if index == offset else 1
            for n in range(start, len(lines) + 1):
                self._check_cancelled()
                if time.monotonic() >= deadline:
                    if mode == "count" and count:
                        results.append({"path": rel, "count": count, "partial": True})
                        total += count
                    return finish(
                        self._continuation(options, index, n, digest), "time_budget"
                    )
                try:
                    matched = matcher.search(lines[n - 1], timeout=0.05) is not None
                except TimeoutError:
                    raise ValueError(
                        "Search expression exceeded its time limit; simplify the query"
                    ) from None
                if not matched:
                    continue
                count += 1
                if mode == "files":
                    break
                if mode == "count":
                    continue
                item = {
                    "path": rel,
                    "line": n,
                    "text": lines[n - 1][:300],
                    "text_truncated": len(lines[n - 1]) > 300,
                }
                if context_lines:
                    item["context"] = [
                        {"line": j, "text": lines[j - 1][:300]}
                        for j in range(
                            max(1, n - context_lines),
                            min(len(lines), n + context_lines) + 1,
                        )
                    ]
                results.append(item)
                total += 1
                if len(results) >= limit:
                    if n == len(lines):
                        next_cursor = self._continuation(options, index + 1)
                    else:
                        next_cursor = self._continuation(options, index, n + 1, digest)
                    return finish(next_cursor, "result_limit")
            if count and mode != "matches":
                results.append(
                    rel if mode == "files" else {"path": rel, "count": count}
                )
                total += count
                if len(results) >= limit:
                    return finish(
                        self._continuation(options, index + 1), "result_limit"
                    )
        return finish()

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
                {
                    "line": n,
                    "text": lines[n - 1][:1000],
                    "text_truncated": len(lines[n - 1]) > 1000,
                }
                for n in range(start, min(end, len(lines)) + 1)
            ],
        }

    def read_files(
        self,
        reads: Annotated[list[ReadRange], Field(min_length=1, max_length=20)],
        max_lines: Annotated[int, Field(ge=1, le=400)] = 400,
        max_chars: Annotated[int, Field(ge=1000, le=18000)] = 12000,
    ):
        ranges = [ReadRange.model_validate(item) for item in reads]
        # Validate all scopes before returning any file data.
        for item in ranges:
            self.path(item.path)
            if item.end < item.start:
                raise ValueError("Invalid line range")
        files, remaining, used_lines = [], [], 0
        for index, item in enumerate(ranges):
            self._check_cancelled()
            if used_lines >= max_lines:
                remaining.extend(x.model_dump() for x in ranges[index:])
                break
            try:
                result = self.read_file(item.path, item.start, item.end)
            except (OSError, UnicodeError, ValueError) as exc:
                result = {"path": item.path, "error": type(exc).__name__}
            lines = result.pop("lines", [])
            result["lines"] = []
            # Reserve room for path/hash/error metadata before adding source lines.
            if len(json.dumps(files + [result], ensure_ascii=True)) > max_chars:
                if not files:
                    raise ValueError(
                        "File metadata exceeds max_chars; increase the read budget"
                    )
                remaining.extend(x.model_dump() for x in ranges[index:])
                break
            files.append(result)
            for line in lines:
                if used_lines >= max_lines:
                    break
                result["lines"].append(line)
                if len(json.dumps(files, ensure_ascii=True)) > max_chars:
                    original = line["text"]
                    line["text"], line["text_truncated"] = "", True
                    if len(json.dumps(files, ensure_ascii=True)) > max_chars:
                        result["lines"].pop()
                        if used_lines == 0:
                            raise ValueError(
                                "Line metadata exceeds max_chars; increase the read budget"
                            )
                        break
                    low, high = 0, len(original)
                    while low < high:
                        mid = (low + high + 1) // 2
                        line["text"] = original[:mid]
                        if len(json.dumps(files, ensure_ascii=True)) <= max_chars:
                            low = mid
                        else:
                            high = mid - 1
                    line["text"] = original[:low]
                used_lines += 1
            if "total_lines" in result:
                next_line = item.start + len(result["lines"])
                if next_line <= min(item.end, result["total_lines"]):
                    remaining.append({**item.model_dump(), "start": next_line})
        return {
            "files": files,
            "remaining": remaining,
            "truncated": bool(remaining),
            "lines_read": used_lines,
            "content_chars": len(json.dumps(files, ensure_ascii=True)),
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
        "find_files": "Find files by glob; continue next_cursor with the same options until null",
        "search_files": "Search literal/regex queries (OR), words and context; matches/files/count modes. Continue next_cursor with unchanged options until null",
        "read_file": "Read bounded lines and obtain the original byte SHA-256",
        "read_files": "Read multiple path/ranges under shared line/character budgets; retry remaining ranges",
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
