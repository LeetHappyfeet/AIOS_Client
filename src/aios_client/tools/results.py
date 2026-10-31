"""Explicit result selection; transmission remains the runner's final report."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResultFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    label: Annotated[str, Field(max_length=160)] = ""
    start: Annotated[int, Field(ge=1)] = 1
    end: Annotated[int, Field(ge=1)] = 200


class ResultText(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    label: Annotated[str, Field(max_length=160)] = "Answer"
    text: Annotated[str, Field(max_length=8000)]


class ResultTools:
    def __init__(self, workspace):
        self.workspace = workspace

    def publish_result(
        self,
        outcome: Literal["completed", "partial", "blocked"],
        summary: Annotated[str, Field(min_length=1, max_length=1000)],
        findings: Annotated[
            list[Annotated[str, Field(max_length=700)]], Field(max_length=6)
        ] = [],
        limitations: Annotated[
            list[Annotated[str, Field(max_length=500)]], Field(max_length=4)
        ] = [],
        files: Annotated[list[ResultFile], Field(max_length=4)] = [],
        content: Annotated[list[ResultText], Field(max_length=4)] = [],
    ):
        if not summary.strip():
            raise ValueError("Result summary must not be blank")
        if len(files) + len(content) > 4:
            raise ValueError("Select at most four file/text content sections")
        sections, receipts = [], []
        for raw in files:
            self.workspace.guard()
            item = ResultFile.model_validate(raw)
            if item.end < item.start or item.end - item.start >= 200:
                raise ValueError("Result file ranges must contain 1–200 lines")
            if any(
                part == ".git" or part == ".env" or part.startswith(".env.")
                for part in PurePosixPath(item.path).parts
            ):
                raise ValueError("Do not publish credential env files or Git metadata")
            path = self.workspace.path(item.path)
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError(
                    "Result excerpts require regular UTF-8 files of at most 2 MiB"
                )
            # Bound the actual read too, in case a concurrent writer grows the file.
            with path.open("rb") as handle:
                data = handle.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("Result file grew beyond its read limit")
            lines = data.decode("utf-8").splitlines(keepends=True)
            selected = self.workspace.redact("".join(lines[item.start - 1 : item.end]))
            provenance = {
                "path": item.path,
                "source_sha256": hashlib.sha256(data).hexdigest(),
                "start_line": item.start,
                "end_line": min(item.end, len(lines)),
                "source_kind": "workspace_file",
            }
            sections.append(
                {
                    **provenance,
                    "label": item.label or item.path,
                    "text": selected[:8000],
                    "source_truncated": len(selected) > 8000,
                }
            )
            receipts.append(provenance)
        for raw in content:
            item = ResultText.model_validate(raw)
            sections.append({**item.model_dump(), "source_kind": "worker_text"})
        # Replace the staged selection only after every source has been read.
        self.workspace.published_result = self.workspace.clean(
            {
                "outcome": outcome,
                "summary": summary,
                "findings": findings,
                "evidence": findings,
                "limitations": limitations,
                "requested_content": sections,
            }
        )
        return {
            "staged": True,
            "delivery": "final_report_after_host_checks",
            "outcome": outcome,
            "content_sections": len(sections),
            "files": receipts,
            "verification": "worker_selection_not_task_verification",
        }


def register(registry, results):
    registry.add(
        "publish_result",
        results.publish_result,
        "Stage findings and selected actual file excerpts or worker text for the final AIOS result; replaces prior selection, does not send immediately or verify success",
        "workspace.results",
        effect="write",
    )
