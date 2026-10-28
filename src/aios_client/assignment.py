"""Workspace protocol v2 assignment contract (kept identical in server/client)."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def relative_path(value):
    if (
        not value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
        or ":" in value
        or ".git" in PurePosixPath(value).parts
    ):
        raise ValueError("Use a relative workspace path without '..'")
    return str(PurePosixPath(value))


class Check(Strict):
    id: str = Field(min_length=1, max_length=80)
    command: str = Field(min_length=1, max_length=8000)
    cwd: str = "."
    timeout_seconds: int = Field(default=120, ge=1, le=1800)
    _path = field_validator("cwd")(relative_path)

    @field_validator("command", "id")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Check command and ID must not be blank")
        return value


class Output(Strict):
    path: str = Field(min_length=1, max_length=500)
    kind: Literal["file", "directory", "absent"] = "file"
    _path = field_validator("path")(relative_path)


class Budgets(Strict):
    model_steps: int = Field(default=60, ge=1, le=200)
    wall_seconds: int = Field(default=1800, ge=1, le=14400)
    context_chars: int = Field(default=100000, ge=1000, le=500000)


class Assignment(Strict):
    objective: str = Field(min_length=1, max_length=16000)
    expected_outputs: list[Output] = Field(default_factory=list, max_length=50)
    required_checks: list[Check] = Field(default_factory=list, max_length=20)
    preserve_paths: list[str] = Field(default_factory=list, max_length=50)
    goal_id: UUID | None = None
    attempt_id: UUID | None = None
    budgets: Budgets = Field(default_factory=Budgets)

    @field_validator("preserve_paths")
    @classmethod
    def paths(cls, values):
        return [relative_path(v) for v in values]

    @model_validator(mode="after")
    def consistent(self):
        if bool(self.goal_id) != bool(self.attempt_id):
            raise ValueError("goal_id and attempt_id must be supplied together")
        if len({c.id for c in self.required_checks}) != len(self.required_checks):
            raise ValueError("Check IDs must be unique")
        if not self.objective.strip():
            raise ValueError("Objective must not be blank")
        if (
            len(json.dumps(self.model_dump(mode="json", exclude={"request_id"})))
            > 20000
        ):
            raise ValueError("Assignment exceeds 20000 character budget")
        return self

    def digest(self):
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


def assignment(value):
    return Assignment.model_validate(
        {"objective": value} if isinstance(value, str) else value
    )


def verification_status(spec, evidence):
    """Check evidence completeness, not the honesty of a remote worker.

    Even a passed result remains worker-supplied evidence requiring goal review.
    """
    if (
        not isinstance(evidence, dict)
        or evidence.get("assignment_hash") != spec.digest()
    ):
        return "incomplete"
    if not evidence.get("snapshot_complete") or not evidence.get("snapshot_hash"):
        return "incomplete"
    if (
        evidence.get("stale") is not False
        or evidence.get("final_hash") != evidence["snapshot_hash"]
    ):
        return "stale"
    if evidence.get("violations"):
        return "failed"
    outputs = evidence.get("outputs", [])
    if not isinstance(outputs, list) or len(outputs) != len(spec.expected_outputs):
        return "incomplete"
    for expected, actual in zip(spec.expected_outputs, outputs):
        if (
            not isinstance(actual, dict)
            or actual.get("path") != expected.path
            or actual.get("kind") != expected.kind
        ):
            return "incomplete"
        if actual.get("passed") is not True:
            return "failed"
    checks = evidence.get("checks", [])
    if not isinstance(checks, list) or len(checks) != len(spec.required_checks):
        return "incomplete"
    for required, actual in zip(spec.required_checks, checks):
        if not isinstance(actual, dict) or any(
            actual.get(k) != v for k, v in required.model_dump().items()
        ):
            return "incomplete"
        if (
            actual.get("snapshot_hash") != evidence["snapshot_hash"]
            or not actual.get("output_complete")
            or not actual.get("log_sha256")
        ):
            return "incomplete"
        if (
            type(actual.get("exit_code")) is not int
            or actual.get("exit_code") != 0
            or actual.get("timed_out") is not False
            or actual.get("cancelled") is not False
        ):
            return "failed"
    return "passed" if checks else "not_configured"
