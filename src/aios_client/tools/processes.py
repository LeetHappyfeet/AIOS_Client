"""Owned process sessions with bounded output, queued stdin, and no restart replay."""

from __future__ import annotations

import codecs
import json
import os
import queue
import threading
import time
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field

from .platforms import ProcessBackend


class ProcessTools:
    def __init__(self, workspace, on_event=lambda *args: None, backend=None):
        self.path, self.artifacts = workspace.path, workspace.artifacts
        self.secrets, self.clean, self.redact = (
            workspace.secrets,
            workspace.clean,
            workspace.redact,
        )
        self.cancelled = workspace.cancelled
        self.processes, self.process_lock = {}, threading.RLock()
        self.command_timeout, self.capture_limit = 120, 8 * 1024 * 1024
        self.backend, self.on_event = backend or ProcessBackend(), on_event

    def run_command(
        self,
        command: Annotated[str, Field(min_length=1, max_length=8000)] | None = None,
        cwd: str = ".",
        argv: Annotated[list[str], Field(min_length=1, max_length=200)] | None = None,
        shell: Literal[
            "default", "sh", "bash", "cmd", "powershell", "pwsh"
        ] = "default",
        stdin: bool = False,
        timeout_seconds: Annotated[float, Field(gt=0, le=1800)] | None = None,
    ):
        with self.process_lock:
            if self.cancelled.is_set():
                raise RuntimeError("Workspace stopped")
            return self._start_command(
                command, cwd, argv, shell, stdin, timeout_seconds
            )

    def _start_command(self, command, cwd, argv, shell, stdin, timeout_seconds):
        if command is not None and (not command or len(command) > 8000):
            raise ValueError("Invalid command length")
        if argv is not None and len(json.dumps(argv)) > 16000:
            raise ValueError("Argument list exceeds size limit")
        if len([x for x in self.processes.values() if not x["done"].is_set()]) >= 4:
            raise ValueError("Four commands are already running")
        pid = str(uuid4())
        log = self.artifacts / (pid + ".log")
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"AIOS_WORKSPACE_REVIEW_KEY", "AIOS_WORKSPACE_OPERATOR_KEY"}
            and not any(s in v for s in self.secrets)
        }
        p = self.backend.launch(command, argv, shell, self.path(cwd), env, stdin)
        record = {
            "p": p,
            "log": log,
            "truncated": False,
            "done": threading.Event(),
            "timed_out": False,
            "cancelled": False,
            "command": command,
            "argv": argv,
            "shell": shell,
            "cwd": cwd,
            "started_at": time.time(),
            "ended_at": None,
            "stdin_enabled": stdin,
            "input_queue": queue.Queue(maxsize=16),
            "input_closed": not stdin,
            "input_error": None,
        }
        self.processes[pid] = record
        try:
            self.on_event(
                "process_started",
                self.clean(
                    {
                        "process_id": pid,
                        "os_pid": p.pid,
                        "command": command,
                        "argv": argv,
                        "shell": shell,
                        "cwd": cwd,
                        "stdin": stdin,
                        "artifact_id": log.name,
                        "started_at": record["started_at"],
                    }
                ),
            )
        except Exception:
            self.backend.stop(record)
            if p.stdin:
                p.stdin.close()
            p.stdout.close()
            record["done"].set()
            raise

        def feed():
            try:
                while True:
                    value = record["input_queue"].get()
                    try:
                        if value is None:
                            return
                        p.stdin.write(value)
                        p.stdin.flush()
                    finally:
                        record["input_queue"].task_done()
            except (OSError, ValueError) as exc:
                record["input_error"] = self.redact(str(exc))[:500]
            finally:
                try:
                    p.stdin.close()
                except (OSError, ValueError):
                    pass

        def drain():
            size = 0
            try:
                with log.open("wb") as f:
                    decoder = codecs.getincrementaldecoder("utf-8")("replace")
                    pending = b""
                    patterns = [x.encode() for x in self.secrets]

                    def emit(data):
                        nonlocal size
                        for secret in patterns:
                            data = data.replace(secret, b"[REDACTED]")
                        remaining = max(0, self.capture_limit - size)
                        captured = data[:remaining]
                        f.write(captured)
                        f.flush()
                        size += len(captured)
                        output = decoder.decode(captured)
                        if output:
                            self.on_event("process_output", {"process_id": pid, "output": output})
                        if len(data) > remaining and not record["truncated"]:
                            record["truncated"] = True
                            self.on_event("process_output_truncated", {
                                "process_id": pid, "capture_limit": self.capture_limit,
                            })

                    while chunk := p.stdout.read1(4096):
                        pending += chunk
                        for secret in patterns:
                            pending = pending.replace(secret, b"[REDACTED]")
                        # Hold only a suffix that could be a split credential. A short
                        # flushed prompt must be visible before the process exits.
                        keep = 0
                        for secret in patterns:
                            for length in range(min(len(secret) - 1, len(pending)), 0, -1):
                                if pending.endswith(secret[:length]):
                                    keep = max(keep, length)
                                    break
                        cutoff = len(pending) - keep
                        emit(pending[:cutoff])
                        pending = pending[cutoff:]
                    emit(pending)
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        self.on_event("process_output", {"process_id": pid, "output": tail})
                p.wait()
            finally:
                p.stdout.close()
                record["ended_at"] = time.time()
                # Wake an idle stdin sender after the child exits.
                if stdin and not record["input_closed"]:
                    try:
                        record["input_queue"].put_nowait(None)
                        record["input_closed"] = True
                    except queue.Full:
                        pass
                try:
                    self.on_event(
                        "process_exited",
                        self.clean(
                            {
                                "process_id": pid,
                                "os_pid": p.pid,
                                "exit_code": p.poll(),
                                "timed_out": record["timed_out"],
                                "cancelled": record["cancelled"],
                                "artifact_id": log.name,
                                "ended_at": record["ended_at"],
                            }
                        ),
                    )
                except Exception as exc:
                    record["journal_error"] = self.redact(str(exc))[:500]
                finally:
                    record["done"].set()

        command_timeout = min(
            self.command_timeout, timeout_seconds or self.command_timeout
        )

        def watchdog():
            if not record["done"].wait(command_timeout):
                record["timed_out"] = True
                self.stop_process(pid, timed_out=True)

        if stdin:
            threading.Thread(target=feed, daemon=True).start()
        threading.Thread(target=drain, daemon=True).start()
        threading.Thread(target=watchdog, daemon=True).start()
        return {
            "process_id": pid,
            "artifact_id": log.name,
            "status": "started",
            "os_pid": p.pid,
            "shell": shell,
            "stdin_enabled": stdin,
        }

    def read_process(self, process_id: str, cursor: Annotated[int, Field(ge=0)] = 0):
        r = self.processes[process_id]
        if cursor < 0:
            raise ValueError("Cursor must be nonnegative")
        if r["log"].exists():
            with r["log"].open("rb") as f:
                f.seek(cursor)
                data = f.read(12000)
        else:
            data = b""
        return {
            "process_id": process_id,
            "output": self.redact(data.decode("utf-8", "replace")),
            "next_cursor": cursor + len(data),
            "exit_code": r["p"].poll(),
            "output_complete": r["done"].is_set(),
            "truncated": r["truncated"],
            "artifact_id": r["log"].name,
            "journal_error": r.get("journal_error"),
            "timed_out": r["timed_out"],
            "cancelled": r["cancelled"],
            "input_pending": r["input_queue"].unfinished_tasks,
            "input_error": r["input_error"],
            "stdin_closed": r["input_closed"],
        }

    def stop_process(self, process_id: str, timed_out: bool = False):
        record = self.processes[process_id]
        p = record["p"]
        if p.poll() is None or not record["done"].is_set():
            record["cancelled"] |= not timed_out
            self.backend.stop(record)
        return {"process_id": process_id, "exit_code": p.poll()}

    def stop_owned_process(self, process_id: str):
        return self.stop_process(process_id)

    def stop(self):
        with self.process_lock:
            self.cancelled.set()
            processes = list(self.processes)
        for pid in processes:
            self.stop_process(pid)

    def write_process(
        self,
        process_id: str,
        text: Annotated[str, Field(max_length=8192)] = "",
        close_stdin: bool = False,
    ):
        record = self.processes[process_id]
        data = text.encode("utf-8")
        if len(data) > 8192:
            raise ValueError("Input exceeds 8192 bytes")
        with self.process_lock:
            if (
                self.cancelled.is_set()
                or record["done"].is_set()
                or record["p"].poll() is not None
            ):
                raise ValueError("Process is no longer accepting input")
            if (
                record["input_closed"]
                or not record["stdin_enabled"]
                or record["input_error"]
            ):
                raise ValueError("Process stdin is closed or unavailable")
            required = bool(data) + bool(close_stdin)
            if record["input_queue"].qsize() + required > 16:
                raise ValueError(
                    "Process input queue is full; read status before sending more"
                )
            if data:
                record["input_queue"].put_nowait(data)
            if close_stdin:
                record["input_queue"].put_nowait(None)
                record["input_closed"] = True
        return {
            "process_id": process_id,
            "input_queued_bytes": len(data),
            "close_queued": close_stdin,
            "delivery": "queued",
        }

    def process_status(self):
        with self.process_lock:
            return {
                "processes": [
                    {
                        "process_id": pid,
                        "os_pid": r["p"].pid,
                        "exit_code": r["p"].poll(),
                        "output_complete": r["done"].is_set(),
                        "command": r["command"],
                        "argv": r["argv"],
                        "shell": r["shell"],
                        "cwd": r["cwd"],
                        "timed_out": r["timed_out"],
                        "cancelled": r["cancelled"],
                        "artifact_id": r["log"].name,
                        "journal_error": r.get("journal_error"),
                    }
                    for pid, r in self.processes.items()
                ]
            }


def register(registry, processes):
    for name, description in {
        "run_command": "Start a workspace command or argv process. Select an available shell; follow its process handle",
        "read_process": "Read bounded UTF-8 output by byte cursor, exit status, cancellation and input status",
        "write_process": "Queue bounded UTF-8 stdin; optionally close stdin. Queued does not mean delivered",
        "stop_process": "Stop an owned process and its child group",
        "process_status": "List owned process handles and observed lifecycle states",
    }.items():
        effect = (
            "execute"
            if name == "run_command"
            else "write"
            if name == "write_process"
            else "control"
            if name == "stop_process"
            else "read"
        )
        registry.add(
            name,
            processes.stop_owned_process
            if name == "stop_process"
            else getattr(processes, name),
            description,
            "workspace.processes",
            effect=effect,
        )

