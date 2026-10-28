from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", "client-data"}


def schema(name, description, properties, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


TOOLS = [
    schema(
        "list_files",
        "List bounded workspace files, skipping dependency and Git directories",
        {"path": {"type": "string"}},
        [],
    ),
    schema(
        "search_files",
        "Search literal text in workspace files; return paths and line numbers",
        {"query": {"type": "string"}},
        ["query"],
    ),
    schema(
        "read_file",
        "Read up to 200 lines and obtain SHA-256 for editing",
        {
            "path": {"type": "string"},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
        },
        ["path"],
    ),
    schema(
        "apply_patch",
        "Replace one exact text occurrence in a file with expected SHA-256; empty hash creates a new file",
        {k: {"type": "string"} for k in ("path", "expected_hash", "old", "new")},
        ["path", "expected_hash", "old", "new"],
    ),
    schema(
        "run_command",
        "Run a command in the workspace. Returns process ID; use read_process to follow it",
        {"command": {"type": "string"}, "cwd": {"type": "string"}},
        ["command"],
    ),
    schema(
        "read_process",
        "Read bounded saved output from a byte cursor",
        {"process_id": {"type": "string"}, "cursor": {"type": "integer"}},
        ["process_id"],
    ),
    schema(
        "stop_process",
        "Stop a running command and its child processes",
        {"process_id": {"type": "string"}},
        ["process_id"],
    ),
    schema("git_status", "Inspect Git status and HEAD", {}),
    schema("git_diff", "Inspect a bounded diff of tracked changes", {}),
]


class Workspace:
    def __init__(self, root: Path, artifacts: Path, secrets=(), command_timeout=120):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be an existing directory")
        self.artifacts = Path(artifacts)
        self.artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.processes = {}
        self.secrets = tuple(s for s in secrets if s)
        self.command_timeout = command_timeout
        self.capture_limit = 8 * 1024 * 1024
        self.cancelled = threading.Event()

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
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        return value

    def path(self, value="."):
        p = (self.root / value).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError("Path escapes workspace")
        return p

    def files(self, relative="."):
        count = 0
        for base, dirs, names in os.walk(self.path(relative), followlinks=False):
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

    def execute(self, name, args):
        spec = next(
            (t["function"] for t in TOOLS if t["function"]["name"] == name), None
        )
        if not spec or not isinstance(args, dict):
            raise ValueError("Unknown tool or invalid arguments")
        props = spec["parameters"]["properties"]
        if set(args) - set(props) or any(
            k not in args for k in spec["parameters"]["required"]
        ):
            raise ValueError("Invalid tool fields")
        for key, value in args.items():
            typ = str if props[key]["type"] == "string" else int
            if type(value) is not typ:
                raise ValueError(f"Invalid type for {key}")
        if self.cancelled.is_set():
            raise RuntimeError("Workspace stopped")
        result = getattr(self, name)(**args)
        # All tool observations are bounded before being journaled or sent to a model.
        encoded = json.dumps(self.clean(result), ensure_ascii=True)
        if len(encoded) > 22000:
            artifact = str(uuid4()) + ".json"
            (self.artifacts / artifact).write_text(encoded)
            return {
                "output_excerpt": encoded[:12000],
                "artifact_id": artifact,
                "truncated": True,
            }
        return json.loads(encoded)

    def list_files(self, path="."):
        paths = []
        for p in self.files(path):
            paths.append(str(p.relative_to(self.root)))
            if len(paths) == 300:
                return {"files": paths, "truncated": True}
        return {"files": paths, "truncated": False}

    def search_files(self, query):
        if not query or len(query) > 500:
            raise ValueError("Search query must be 1–500 characters")
        matches = []
        for p in self.files():
            if p.stat().st_size > 1024 * 1024:
                continue
            try:
                for n, line in enumerate(p.read_text().splitlines(), 1):
                    if query in line:
                        matches.append(
                            {
                                "path": str(p.relative_to(self.root)),
                                "line": n,
                                "text": line[:300],
                            }
                        )
                        if len(matches) == 40:
                            return {"matches": matches, "truncated": True}
            except (UnicodeError, OSError):
                continue
        return {"matches": matches, "truncated": False}

    def read_file(self, path, start=1, end=200):
        p = self.path(path)
        if p.stat().st_size > 2 * 1024 * 1024:
            raise ValueError(
                "File exceeds 2 MiB read limit; use a command to select a section"
            )
        data = p.read_bytes()
        lines = data.decode("utf-8").splitlines()
        if start < 1 or end < start:
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

    def apply_patch(self, path, expected_hash, old, new):
        p = self.path(path)
        if len(new.encode()) > 200_000:
            raise ValueError("Edit exceeds size limit")
        data = p.read_bytes() if p.exists() else b""
        actual = hashlib.sha256(data).hexdigest() if p.exists() else ""
        if actual != expected_hash:
            raise ValueError("File changed; read it again before editing")
        if not p.exists():
            if old:
                raise ValueError("New file requires empty old text")
            result = new
        else:
            text = data.decode("utf-8")
            if not old or text.count(old) != 1:
                raise ValueError("Old text must match exactly once")
            result = text.replace(old, new, 1)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(result)
        return {
            "path": path,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "edited": True,
        }

    def run_command(self, command, cwd="."):
        if not command or len(command) > 8000:
            raise ValueError("Invalid command length")
        if len([x for x in self.processes.values() if x["p"].poll() is None]) >= 4:
            raise ValueError("Four commands are already running")
        pid = str(uuid4())
        log = self.artifacts / (pid + ".log")
        env = {
            k: v for k, v in os.environ.items() if not any(s in v for s in self.secrets)
        }
        p = subprocess.Popen(
            command,
            shell=True,
            cwd=self.path(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        record = {"p": p, "log": log, "truncated": False, "done": threading.Event()}
        self.processes[pid] = record

        def drain():
            size = 0
            try:
                with log.open("wb") as f:
                    pending = b""
                    patterns = [x.encode() for x in self.secrets]
                    keep = max([len(x) for x in patterns] + [1])

                    def emit(data):
                        nonlocal size
                        for secret in patterns:
                            data = data.replace(secret, b"[REDACTED]")
                        remaining = max(0, self.capture_limit - size)
                        f.write(data[:remaining])
                        f.flush()
                        size += len(data[:remaining])
                        record["truncated"] |= len(data) > remaining

                    while chunk := p.stdout.read1(4096):
                        pending += chunk
                        cutoff = max(0, len(pending) - keep)
                        for secret in patterns:
                            start = pending.find(secret)
                            while start >= 0:
                                if start < cutoff < start + len(secret):
                                    cutoff = start
                                start = pending.find(secret, start + 1)
                        emit(pending[:cutoff])
                        pending = pending[cutoff:]
                    emit(pending)
                p.wait()
            finally:
                p.stdout.close()
                record["done"].set()

        def watchdog():
            if not record["done"].wait(self.command_timeout):
                self.stop_process(pid)

        threading.Thread(target=drain, daemon=True).start()
        threading.Thread(target=watchdog, daemon=True).start()
        return {"process_id": pid, "artifact_id": log.name, "status": "started"}

    def read_process(self, process_id, cursor=0):
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
        }

    def stop_process(self, process_id):
        record = self.processes[process_id]
        p = record["p"]
        if p.poll() is None or not record["done"].is_set():
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                    if not record["done"].wait(2):
                        os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)
        return {"process_id": process_id, "exit_code": p.poll()}

    def stop(self):
        self.cancelled.set()
        for pid in list(self.processes):
            self.stop_process(pid)

    def git_status(self):
        return self._git(["status", "--short"]) | {
            "head": self._git(["rev-parse", "HEAD"])["output"].strip()
        }

    def git_diff(self):
        return self._git(["diff", "--no-ext-diff", "--no-textconv", "--stat"]) | {
            "diff": self._git(["diff", "--no-ext-diff", "--no-textconv"])["output"]
        }

    def _git(self, args):
        # Use the bounded process capture path even for Git: large diffs stay on disk.
        import shlex

        command = (
            subprocess.list2cmdline(["git", *args])
            if os.name == "nt"
            else shlex.join(["git", *args])
        )
        r = self.run_command(command)
        p = self.processes[r["process_id"]]
        p["done"].wait(self.command_timeout + 3)
        return self.read_process(r["process_id"])
