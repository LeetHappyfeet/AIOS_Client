"""Native model/tool iteration without knowledge of AIOS routes or credentials."""

from __future__ import annotations

import json
import time

from ..tools.registry import ToolArguments
from ..assignment import requires_read_only
from .context import RepositoryInstructions, WorkingContext
from .tasks import TaskBlocked, TaskTools


class AgentSession:
    def __init__(
        self,
        *,
        model,
        model_id,
        workspace,
        spec,
        system,
        journal,
        run_id,
        control,
        gate,
        deadline,
        progress,
        observe,
    ):
        self.model, self.model_id, self.workspace, self.spec = (
            model,
            model_id,
            workspace,
            spec,
        )
        self.journal, self.run_id = journal, run_id
        self.control, self.gate, self.deadline = control, gate, deadline
        self.progress, self.observe = progress, observe
        self.registry = workspace.registry
        self.registry.read_only = self.registry.read_only or requires_read_only(spec.objective)
        if self.registry.read_only:
            system += "\nRead-only inspection is enforced: use inspect_environment/file reads; return JSON directly without creating files or launching commands."
        self.instructions = RepositoryInstructions(workspace)
        self.context = WorkingContext(
            system,
            spec.model_dump(mode="json"),
            spec.budgets.context_chars,
            self.instructions,
            workspace.clean,
        )
        self.tasks = TaskTools(self.record, progress, workspace.stop)
        self.tasks.register(self.registry)
        self.seen_calls = set()
        self.response_recoveries = 0

    def recover_response(self, reason):
        # A rejected response has executed no calls. Do not put its incomplete
        # tool-call batch into history or replay an earlier successful batch.
        self.record("model_response_rejected", {"reason": reason})
        if self.response_recoveries >= 2:
            raise ValueError(reason + "; model response recovery limit reached")
        self.response_recoveries += 1
        self.context.append({"role": "user", "content": (
            reason + ". The rejected response executed no tools. Earlier tool receipts remain valid; "
            "do not repeat completed operations. Return a concise final JSON report using observed "
            "findings, marking limitations, or one small complete tool call if more evidence is required. "
            "Do not create a file merely to return your answer."
        )})
        self.checkpoint()

    def record(self, kind, body):
        self.journal.event(self.run_id, kind, self.workspace.clean(body))

    def checkpoint(self):
        self.journal.save_state(
            self.run_id,
            {
                "version": 1,
                "assignment_hash": self.spec.digest(),
                "seen_calls": sorted(self.seen_calls),
                "plan": self.tasks.plan,
                "blocked_reason": self.tasks.blocked,
                "published_result": self.workspace.published_result,
                "context": self.context.snapshot(),
            },
        )

    def check(self):
        if time.monotonic() >= self.deadline:
            raise RuntimeError("Assignment wall time budget reached")
        self.workspace.guard()
        self.control()
        self.workspace.guard()

    def run(self):
        for step in range(self.spec.budgets.model_steps):
            self.check()
            records = self.workspace.process_status()["processes"]
            active = [r for r in records if not r["output_complete"]]
            recent = [r for r in records if r["output_complete"]][-8:]
            process_state = [
                {
                    k: r[k]
                    for k in (
                        "process_id",
                        "os_pid",
                        "exit_code",
                        "output_complete",
                        "timed_out",
                        "cancelled",
                        "artifact_id",
                    )
                }
                for r in active + recent
            ]
            self.context.state = {
                "plan": self.tasks.plan,
                "plan_explanation": self.tasks.explanation,
                "processes": process_state,
                "host": self.workspace.host_info(),
                "result_staged": self.workspace.published_result is not None,
            }
            messages = self.context.prepare()
            presented_guidance = {
                path: item["sha256"] for path, item in self.instructions.loaded.items()
            }
            self.checkpoint()
            while not self.gate.acquire(timeout=0.2):
                self.check()
            try:
                self.check()
                response = self.model.completion(
                    self.model_id,
                    messages,
                    self.registry.catalog(),
                    timeout=min(
                        float(self.model.http.timeout.read or 120),
                        max(0.01, self.deadline - time.monotonic()),
                    ),
                )
            finally:
                self.gate.release()
            self.check()
            diagnostics = response.get("_diagnostics") or {}
            diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
            self.workspace.model_diagnostics = self.workspace.clean(diagnostics)
            raw = json.dumps(self.workspace.clean({k: response.get(k) for k in ("content", "tool_calls")}), ensure_ascii=True)
            self.record("model_response", {
                "step": step + 1, **diagnostics,
                "message_excerpt": raw[:16000], "message_truncated": len(raw) > 16000,
            })
            if diagnostics.get("finish_reason") == "length":
                self.recover_response("Provider response reached its output token limit (finish_reason=length)")
                continue
            if diagnostics.get("finish_reason") == "content_filter":
                raise ValueError("Provider stopped the response with finish_reason=content_filter")
            calls = response.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > 8:
                raise ValueError("Model returned an invalid tool-call batch")
            assistant = {
                "role": "assistant",
                "content": self.workspace.redact(response.get("content") or ""),
            }
            if not calls and not assistant["content"].strip():
                self.recover_response("Model returned neither tool calls nor a final report")
                continue
            if not calls and assistant["content"].lstrip().startswith("{"):
                try:
                    json.loads(assistant["content"])
                except ValueError:
                    self.recover_response("Model returned incomplete or malformed final JSON")
                    continue
            if calls:
                batch_ids = set()
                for call in calls:
                    if not isinstance(call, dict) or not isinstance(
                        call.get("function"), dict
                    ):
                        raise ValueError("Model returned an invalid native tool call")
                    function = call["function"]
                    if not isinstance(function.get("name"), str) or not isinstance(
                        function.get("arguments"), str
                    ):
                        raise ValueError(
                            "Native tool calls require a name and JSON argument string"
                        )
                    call_id = call.get("id")
                    if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
                        raise ValueError("Tool call has no valid ID")
                    if call_id in self.seen_calls or call_id in batch_ids:
                        raise ValueError(
                            "Model repeated a tool-call ID; refusing to replay it"
                        )
                    batch_ids.add(call_id)
                assistant["tool_calls"] = calls
            self.context.append(assistant)
            self.record("assistant", assistant)
            if not calls:
                if any(
                    not r["done"].is_set() for r in self.workspace.processes.values()
                ):
                    self.context.append(
                        {
                            "role": "user",
                            "content": "Commands are still running. Inspect their results before finishing.",
                        }
                    )
                    continue
                self.checkpoint()
                return assistant["content"]
            for call in calls:
                self.check()
                call_id = call["id"]
                self.seen_calls.add(call_id)
                name = call.get("function", {}).get("name", "")
                try:
                    raw = call["function"]["arguments"]
                    if not isinstance(raw, str) or len(raw) > 250000:
                        raise ToolArguments("Tool arguments exceed the message limit")
                    args = json.loads(raw)
                    self.instructions.for_tool(args, name)
                    tool = self.registry.tools.get(name)
                    unseen = [
                        path
                        for path, item in self.instructions.loaded.items()
                        if presented_guidance.get(path) != item["sha256"]
                    ]
                    if tool and tool.effect in {"write", "execute"} and unseen:
                        raise ToolArguments(
                            "Review newly loaded repository guidance before retrying this operation with a new call ID: "
                            + ", ".join(unseen)
                        )
                    self.record(
                        "tool_started", {"id": call_id, "name": name, "arguments": args}
                    )
                    self.workspace.command_timeout = min(
                        120, max(0.01, self.deadline - time.monotonic())
                    )
                    self.check()
                    observation = self.registry.execute(name, args)
                except (
                    ToolArguments,
                    ValueError,
                    KeyError,
                    OSError,
                    UnicodeError,
                ) as exc:
                    observation = {
                        "ok": False,
                        "error": {
                            "code": type(exc).__name__,
                            "message": self.workspace.redact(str(exc))[:1000],
                        },
                    }
                self.record(
                    "tool_result", {"id": call_id, "name": name, "result": observation}
                )
                if observation.get("ok") and name not in {
                    "run_command", "update_plan", "publish_result", "aios_progress", "aios_recall",
                } and observation.get("output_complete", True):
                    receipt = json.dumps(self.workspace.clean(observation), ensure_ascii=True)
                    self.workspace.partial_findings = (
                        self.workspace.partial_findings + [name + ": " + receipt[:650]
                            + (" [receipt excerpt truncated]" if len(receipt) > 650 else "")]
                    )[-6:]
                self.context.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(observation),
                    }
                )
                self.checkpoint()
                if self.tasks.blocked:
                    try:
                        self.observe(name, call_id, observation)
                    except RuntimeError:
                        self.record(
                            "delivery_pending",
                            {"message": "Blocked observation retained for delivery"},
                        )
                    raise TaskBlocked(self.tasks.blocked)
                self.observe(name, call_id, observation)
        raise RuntimeError("Assignment model step budget reached")
