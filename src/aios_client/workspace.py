"""Coding-workspace facade. Tool execution is independent of AIOS transport."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from pydantic import Field

from .tools import files, git, processes
from .tools.registry import Registry

EXCLUDED = files.EXCLUDED


class Workspace:
    def __init__(
        self,
        root: Path,
        artifacts: Path,
        secrets=(),
        command_timeout=120,
        on_event=lambda *args: None,
        process_backend=None,
    ):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be an existing directory")
        self.artifacts = Path(artifacts).resolve()
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.secrets = tuple(s for s in secrets if s)
        self.cancelled = threading.Event()
        self.filesystem = files.FileTools(self.root, self.cancelled)
        self.process_manager = processes.ProcessTools(
            self, on_event, backend=process_backend
        )
        self.process_manager.command_timeout = command_timeout
        self.git = git.GitTools(self)
        self.registry = Registry(self.guard, self.bound_result)
        files.register(self.registry, self.filesystem)
        processes.register(self.registry, self.process_manager)
        git.register(self.registry, self.git)
        self.registry.add(
            "host_info",
            self.host_info,
            "Inspect OS, available shells, process capabilities and workspace scope",
            "host.inspect",
        )
        self.registry.add(
            "read_artifact",
            self.read_artifact,
            "Read a bounded retained tool result or command log by byte cursor",
            "workspace.artifacts",
        )

    def __getattr__(self, name):
        # Retain the public facade used by host verification and v2 integrations.
        for component in ("filesystem", "process_manager", "git"):
            service = self.__dict__.get(component)
            if (
                service is not None
                and not name.startswith("_")
                and hasattr(service, name)
            ):
                return getattr(service, name)
        raise AttributeError(name)

    @property
    def processes(self):
        return self.process_manager.processes

    @property
    def command_timeout(self):
        return self.process_manager.command_timeout

    @command_timeout.setter
    def command_timeout(self, value):
        self.process_manager.command_timeout = value

    @property
    def capture_limit(self):
        return self.process_manager.capture_limit

    @capture_limit.setter
    def capture_limit(self, value):
        self.process_manager.capture_limit = value

    def path(self, value="."):
        return self.filesystem.path(value)

    def guard(self):
        if self.cancelled.is_set():
            raise RuntimeError("Workspace stopped")

    def redact(self, value):
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def clean(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        return value

    def bound_result(self, result):
        encoded = json.dumps(self.clean(result), ensure_ascii=True)
        if len(encoded) > 22000:
            artifact = str(uuid4()) + ".json"
            (self.artifacts / artifact).write_text(encoded, encoding="utf-8")
            metadata = {
                k: result[k]
                for k in ("status", "knowledge_revision")
                if k in result
                and isinstance(result[k], (str, int, float, bool, type(None)))
                and len(str(result[k])) < 100
            }
            return {
                **metadata,
                "ok": result.get("ok", True),
                "output_excerpt": encoded[:12000],
                "artifact_id": artifact,
                "truncated": True,
            }
        return json.loads(encoded)

    def execute(self, name, args):
        return self.registry.execute(name, args)

    def host_info(self):
        return {
            **self.process_manager.backend.capabilities(),
            "workspace": str(self.root),
            "tool_capabilities": sorted(
                {t.capability for t in self.registry.tools.values()}
            ),
            "tool_protocol": 1,
        }

    def read_artifact(
        self,
        artifact_id: str,
        cursor: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int, Field(ge=1, le=12000)] = 12000,
    ):
        if (
            not artifact_id
            or Path(artifact_id).name != artifact_id
            or "\\" in artifact_id
            or ":" in artifact_id
        ):
            raise ValueError("Use a retained artifact basename")
        path = self.artifacts / artifact_id
        if path.is_symlink() or not path.is_file():
            raise ValueError("Artifact is unavailable")
        with path.open("rb") as f:
            f.seek(cursor)
            data = f.read(limit)
        return {
            "artifact_id": artifact_id,
            "output": self.redact(data.decode("utf-8", "replace")),
            "next_cursor": cursor + len(data),
            "complete": cursor + len(data) >= path.stat().st_size,
        }

    def stop(self):
        self.process_manager.stop()
