"""Deterministic inspection of retained, redacted owned-process output."""

from __future__ import annotations

import hashlib
import time
from typing import Annotated

import regex
from pydantic import Field

MAX_LOG_BYTES = 8 * 1024 * 1024
Query = Annotated[str, Field(min_length=1, max_length=500)]


class OutputTools:
    def __init__(self, workspace):
        self.workspace = workspace

    def _snapshot(self, process_id):
        record = self.workspace.processes[process_id]
        self.workspace.guard()
        try:
            with record["log"].open("rb") as handle:
                data = handle.read(MAX_LOG_BYTES)
                more = bool(handle.read(1))
        except FileNotFoundError:
            data, more = b"", False
        metadata = {
            "process_id": process_id,
            "artifact_id": record["log"].name,
            "exit_code": record["p"].poll(),
            "output_complete": record["done"].is_set(),
            "timed_out": record["timed_out"],
            "cancelled": record["cancelled"],
            "capture_truncated": record["truncated"],
            "scan_truncated": more,
            "snapshot_bytes": len(data),
            "snapshot_sha256": hashlib.sha256(data).hexdigest(),
        }
        # Line numbers describe this captured text snapshot (including CR progress
        # lines), not independent stdout/stderr streams or a terminal screen.
        return metadata, data.decode("utf-8", "replace").splitlines()

    @staticmethod
    def _line(lines, index):
        text = lines[index]
        return {
            "line": index + 1,
            "text": text[:500],
            "text_truncated": len(text) > 500,
        }

    def search_process_output(
        self,
        process_id: str,
        query: Query,
        start_line: Annotated[int, Field(ge=1)] = 1,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        context_lines: Annotated[int, Field(ge=0, le=3)] = 1,
        case_sensitive: bool = False,
        regex_mode: bool = False,
    ):
        metadata, lines = self._snapshot(process_id)
        try:
            matcher = regex.compile(
                query if regex_mode else regex.escape(query),
                0 if case_sensitive else regex.IGNORECASE,
            )
        except regex.error:
            raise ValueError("Invalid output search expression") from None
        deadline = time.monotonic() + min(2, self.workspace.command_timeout)
        matches, next_line = [], None
        for index in range(start_line - 1, len(lines)):
            self.workspace.guard()
            if time.monotonic() >= deadline:
                next_line = index + 1
                break
            try:
                found = matcher.search(lines[index], timeout=0.05)
            except TimeoutError:
                raise ValueError(
                    "Output search expression exceeded its time limit"
                ) from None
            if not found:
                continue
            item = self._line(lines, index)
            if context_lines:
                item["context"] = [
                    self._line(lines, i)
                    for i in range(
                        max(0, index - context_lines),
                        min(len(lines), index + context_lines + 1),
                    )
                ]
            matches.append(item)
            if len(matches) >= limit:
                next_line = index + 2 if index + 1 < len(lines) else None
                break
        return {
            **metadata,
            "matches": matches,
            "next_line": next_line,
            "total_lines": len(lines),
            "search_complete": next_line is None and not metadata["scan_truncated"],
            "scope": "captured_output_snapshot",
            "truncated": next_line is not None or metadata["scan_truncated"],
        }

    def process_output_summary(
        self,
        process_id: str,
        head_lines: Annotated[int, Field(ge=0, le=10)] = 3,
        tail_lines: Annotated[int, Field(ge=0, le=10)] = 5,
        terms: Annotated[
            list[Annotated[str, Field(min_length=1, max_length=100)]],
            Field(max_length=8),
        ] = ["error", "warning", "failed", "traceback"],
    ):
        metadata, lines = self._snapshot(process_id)
        counts = {term: 0 for term in terms}
        samples = []
        deadline = time.monotonic() + min(2, self.workspace.command_timeout)
        scanned = 0
        for index, line in enumerate(lines):
            self.workspace.guard()
            if time.monotonic() >= deadline:
                break
            folded = line.casefold()
            hits = [term for term in counts if term.casefold() in folded]
            for term in hits:
                counts[term] += 1
            if hits and len(samples) < 8:
                samples.append({**self._line(lines, index), "terms": hits})
            scanned += 1
        return {
            **metadata,
            "total_lines": len(lines),
            "head": [self._line(lines, i) for i in range(min(head_lines, len(lines)))],
            "tail": [
                self._line(lines, i)
                for i in range(max(0, len(lines) - tail_lines), len(lines))
            ],
            "matching_line_counts": counts,
            "samples": samples,
            "counted_lines": scanned,
            "counts_complete": scanned == len(lines) and not metadata["scan_truncated"],
            "interpretation": "Literal term matches are observations, not a success/failure verdict",
        }


def register(registry, output):
    registry.add(
        "search_process_output",
        output.search_process_output,
        "Search retained owned-command output; return bounded context and next_line for this snapshot",
        "workspace.processes",
    )
    registry.add(
        "process_output_summary",
        output.process_output_summary,
        "Inspect command exit status, head/tail and literal error-term counts without another model call",
        "workspace.processes",
    )
