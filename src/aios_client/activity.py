"""Local operator visibility. These records are never sent to AIOS."""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler


def terminal_text(value):
    # Escape terminal control sequences, including CR, instead of executing them.
    return re.sub(
        r"[\x00-\x08\x0b-\x1f\x7f-\x9f]",
        lambda m: repr(m[0])[1:-1],
        str(value),
    )


class ActivityLog:
    def __init__(self, directory, agent, secrets=(), stream=None):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "activity.log"
        self.secrets = tuple(s for s in secrets if s)
        self.agent = agent
        self.lock = threading.RLock()
        self.logger = logging.Logger("aios-client.activity", level=logging.INFO)
        formatter = logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
        )
        # Per-agent retention: current file plus three backups (about 20 MiB).
        file = RotatingFileHandler(
            self.path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        self.path.chmod(0o600)
        console = logging.StreamHandler(sys.stderr if stream is None else stream)
        for handler in (console, file):
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def write(self, run, kind, text):
        value = str(text)
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        prefix = f"[agent={terminal_text(self.agent)} run={run}] {kind}"
        with self.lock:
            # A terminal newline ends the last line; it is not an extra empty record.
            lines = terminal_text(value).split("\n")
            if lines[-1] == "":
                lines.pop()
            for line in lines:
                self.logger.info("%s %s", prefix, line)

    def tool_result(self, run, name, call_id, observation):
        """Show receipts without repeating file contents or command output."""
        keys = (
            "ok",
            "error",
            "path",
            "edited",
            "sha256",
            "files",
            "artifact_id",
            "process_id",
            "exit_code",
            "timed_out",
            "cancelled",
            "truncated",
            "complete",
            "output_complete",
            "next_cursor",
            "next_line",
            "status_code",
            "http_success",
            "body_truncated",
            "capture_truncated",
            "staged",
            "delivery",
            "exists",
            "kind",
            "status",
            "is_repository",
        )
        detail = {k: observation[k] for k in keys if k in observation}
        if isinstance(detail.get("files"), list) and len(detail["files"]) > 20:
            detail["file_count"] = len(detail["files"])
            detail["files"] = detail["files"][:20]
            detail["files_truncated"] = True
        self.write(
            run,
            "tool_result",
            f"{name} call={call_id} " + json.dumps(detail, ensure_ascii=False),
        )

    def process(self, run, kind, body):
        pid = body.get("process_id", "?")
        if kind == "process_output":
            self.write(run, f"output process={pid}", body["output"])
        else:
            self.write(run, kind, json.dumps(body, ensure_ascii=False))

    def close(self):
        with self.lock:
            for handler in self.logger.handlers[:]:
                handler.close()
                self.logger.removeHandler(handler)


class ProgressLimiter:
    """Bound model-generated reporting independently of prompt compliance."""

    def __init__(self, clock, interval=30, limit=20):
        self.clock, self.interval, self.limit = clock, interval, limit
        self.seen, self.count, self.last = set(), 0, None

    def admit(self, kind, summary, force=False):
        key = (kind, summary)
        now = self.clock()
        if key in self.seen:
            return False
        if not force:
            if self.count >= self.limit:
                return False
            if (
                kind != "blocked"
                and self.last is not None
                and now - self.last < self.interval
            ):
                return False
            self.count += 1
        self.seen.add(key)
        self.last = now
        return True
