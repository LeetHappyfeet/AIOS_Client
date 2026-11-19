"""Local worker status shared by foreground, browser and standalone workers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_REFRESH_SECONDS = 3
_STALE_SECONDS = 15
_CONSOLE_LOCK = threading.RLock()

PHASE_LABELS = {
    "connecting": "Connecting to AIOS",
    "recovering": "Recovering local runs and queued reports",
    "listening": "Listening for assignments",
    "needs_authorization": "Awaiting authorization in AIOS Workspace Delegation",
    "waiting_workspace": "Waiting for shared workspace or host administration lock",
    "retrying_aios": "Retrying AIOS connection",
    "preparing": "Assignment claimed",
    "waiting_model_slot": "Waiting for model slot",
    "waiting_model_response": "Waiting for model response",
    "running_tool": "Running tool",
    "verifying": "Verifying configured checks",
    "finalizing": "Closing run and lease monitor",
    "delivering": "Delivering report to AIOS",
    "between_runs": "Run ended",
    "failed": "Worker failed",
    "stopping": "Stopping",
    "stopped": "Worker stopped",
}


def console_line(message):
    with _CONSOLE_LOCK:
        try:
            print(message, file=sys.stderr, flush=True)
        except OSError:
            pass


def read_live(directory):
    try:
        snapshot = json.loads((directory / "live.json").read_text(encoding="utf-8"))
        now = time.time()
        elapsed = now - snapshot["updated_at"]
        active = snapshot["active"]
        snapshot["elapsed_seconds"] = max(0, int(now - snapshot["phase_since"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if active and not 0 <= elapsed <= _STALE_SECONDS:
        # A killed worker may leave a snapshot behind without a final transition.
        return None
    return snapshot


def format_phase(phase, detail=None):
    label = PHASE_LABELS.get(phase, phase.replace("_", " ").capitalize())
    return f"{label} · {detail}" if detail else label


class LiveStatus:
    def __init__(self, directory, agent, clean, concise=False):
        self.directory = directory
        self.agent = clean(agent)
        self.clean = clean
        self.concise = concise
        self.lock = threading.RLock()
        self.snapshot = {}
        self.shutting_down = False
        self.write_warned = False

    def _publish(self):
        self.snapshot["updated_at"] = time.time()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.directory, mode="w", encoding="utf-8", delete=False
            ) as output:
                temporary = Path(output.name)
                json.dump(self.snapshot, output)
            os.replace(temporary, self.directory / "live.json")
        except OSError:
            if not self.write_warned:
                self.write_warned = True
                self.notice("Could not publish live status to the local data directory")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def notice(self, message):
        if self.concise:
            console_line(f"{time.strftime('%H:%M:%S')}  {self.agent}  {self.clean(message)}")

    @contextmanager
    def track(self):
        # The caller holds the per-agent run lock for this entire lifetime.
        with self.lock:
            self.snapshot = {"active": True, "pid": os.getpid(), "commands": {}}
            self.set_phase("connecting")
        end = threading.Event()

        def refresh():
            while not end.wait(_REFRESH_SECONDS):
                with self.lock:
                    self._publish()

        thread = threading.Thread(target=refresh, name="aios-live-status", daemon=True)
        thread.start()
        try:
            yield
        except Exception as exc:
            if not self.shutting_down:
                self.set_phase("failed", str(exc))
            raise
        finally:
            end.set()
            thread.join()
            with self.lock:
                self.snapshot["active"] = False
                self.snapshot.update(run_id=None, objective=None, commands={})
                if self.snapshot["phase"] == "failed":
                    self._publish()
                else:
                    self.set_phase("stopped")

    def set_phase(self, phase, detail=None):
        with self.lock:
            if not self.snapshot:
                return
            if self.shutting_down and phase not in {"stopping", "stopped"}:
                if phase not in {"finalizing", "delivering"}:
                    return
                phase, detail = "stopping", format_phase(phase, detail)
            detail = self.clean(detail) if detail else None
            if (phase, detail) == (self.snapshot.get("phase"), self.snapshot.get("detail")):
                return
            self.snapshot.update(
                phase=phase, detail=detail, phase_since=time.time(),
                label=format_phase(phase, detail),
            )
            self._publish()
            self.notice(self.snapshot["label"])

    def polled(self, phase, *, transition=True):
        with self.lock:
            if self.snapshot.get("active"):
                self.snapshot.update(last_poll_at=time.time(), listener_status=phase)
                if transition:
                    self.set_phase(phase)

    def assignment(self, run_id, objective):
        with self.lock:
            if self.snapshot.get("active"):
                self.snapshot.update(run_id=self.clean(run_id), objective=self.clean(objective))
                self.set_phase("preparing", objective)

    def process(self, kind, body):
        if kind not in {"process_started", "process_exited", "process_output_truncated"}:
            return
        with self.lock:
            if not self.snapshot.get("active"):
                return
            process_id = self.clean(body["process_id"])
            commands = self.snapshot["commands"]
            if kind == "process_started":
                command = self.clean(body.get("command") or body.get("argv"))
                commands[process_id] = {"command": command, "started_at": body["started_at"]}
                self.notice(f"Command started · {process_id} · {command}")
            elif kind == "process_exited":
                commands.pop(process_id, None)
                outcome = f"exit {body['exit_code']}"
                if body.get("timed_out"):
                    outcome += " · timed out"
                if body.get("cancelled"):
                    outcome += " · cancelled"
                self.notice(f"Command exited · {process_id} · {outcome}")
            else:
                self.notice(f"Command output truncated · {process_id}")
                return
            self._publish()

    def tool_result(self, name, observation):
        error = observation.get("error")
        if error:
            if isinstance(error, dict):
                error = error.get("message") or error.get("code") or "unknown error"
            self.notice(f"Tool error · {name} · {error}")
        elif name == "research_search" and isinstance(observation.get("results"), list):
            count = len(observation["results"])
            source = observation.get("source_id") or "research sources"
            self.notice(f"Search · {source} · {count} candidates · {observation.get('status', 'unknown')}")
        elif name == "research_read" and observation.get("passage_retrieved"):
            title = (observation.get("evidence") or {}).get("title", "source")
            self.notice(f"Read passage · {observation.get('section', 'unknown section')} · {title}")

    def finished_run(self, status, error=None):
        if status:
            detail = f"Execution {status['execution']} · Checks {status['verification']}"
            if status["worker_outcome"]:
                detail += f" · Worker outcome {status['worker_outcome']}"
            self.notice(f"{detail} · AIOS report {status['delivery']}")
        if error:
            self.notice(f"Run error · {error}")
        with self.lock:
            if self.snapshot.get("active"):
                self.snapshot.update(run_id=None, objective=None, commands={})
                if self.shutting_down:
                    self._publish()
                else:
                    self.set_phase("between_runs")

    def stop(self):
        with self.lock:
            if not self.snapshot.get("active") or self.shutting_down:
                return
            self.shutting_down = True
            phase = self.snapshot.get("phase", "connecting")
            detail = format_phase(phase, self.snapshot.get("detail"))
            if self.snapshot["commands"]:
                detail += " · terminating owned commands"
            self.set_phase("stopping", detail)
            if not self.concise:
                console_line(f"{self.agent} · {self.snapshot['label']}")
