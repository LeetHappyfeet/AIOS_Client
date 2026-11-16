"""Native model/tool iteration without knowledge of AIOS routes or credentials."""

from __future__ import annotations

import json
import time

from ..tools.registry import ToolArguments
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
        worker_brief=None,
        legacy_hud=None,
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
        self.hud_chars = len(worker_brief or legacy_hud or "")
        if legacy_hud is not None:
            system += "\nAIOS context:\n" + legacy_hud
        if spec.required_checks:
            system += (
                "\nThe host runs the configured required_checks automatically after your final response. "
                "Leave report-format checks to the host and verify the actual deliverables during work."
            )
        self.registry = workspace.registry
        self.registry.read_only = self.registry.read_only or spec.inspection_only()
        if self.registry.read_only:
            system += "\nRead-only inspection is enforced: use inspect_environment, inspect_privileges and file reads; return JSON directly without creating files or launching commands."
        self.instructions = RepositoryInstructions(workspace)
        self.context = WorkingContext(
            system,
            spec.model_dump(mode="json"),
            spec.budgets.context_chars,
            self.instructions,
            workspace.clean,
            worker_brief=worker_brief,
            retain_result=workspace.retain_tool_result,
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

    def research_fallback(self, reason):
        """Produce an auditable outcome, never invented geological findings."""
        control = self.workspace.research_tools.control
        items = []
        for reference in control.verified_passages[-12:]:
            item = self.workspace.research_tools.evidence.get(reference["evidence_id"], {})
            public = item.get("public", {})
            items.append({
                "evidence_id": reference["evidence_id"], "source_id": public.get("source_id"),
                "title": public.get("title"), "section": reference["section"],
                "note": "Passage retrieved; no independent claim verified",
            })
        outcome = "partial" if items else "blocked"
        report = {
            "objective": getattr(getattr(self, "spec", None), "objective", "Research assignment"),
            "outcome": outcome,
            "summary": ("Research ended without a completed synthesis. "
                        + str(len(items)) + " distinct passage references were retrieved; "
                        "the original research findings were not established."),
            "evidence": items,
            "findings": [],  # Retrieved passages are not validated research conclusions.
            "limitations": [reason, "No model-authored final synthesis was produced.",
                            "Retrieved sources are not independent proof of any claim."],
            "artifacts": [], "requested_content": [],
            "research_progress": control.snapshot(),
        }
        self.record("research_partial_report", {"reason": reason,
                    "evidence_count": len(items), "outcome": outcome})
        return json.dumps(report, ensure_ascii=False)

    def run(self):
        research = (getattr(self.registry, "tool_mode", "workspace") == "research"
                    and bool(self.workspace.research_tools.sources))
        step_limit = min(self.spec.budgets.model_steps, 20) if research else self.spec.budgets.model_steps
        report_steps = min(3, max(1, step_limit // 4))
        for step in range(step_limit):
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
                **({"research_progress": self.workspace.research_tools.control.snapshot(),
                    "research_source_health": self.workspace.research_tools.source_failures}
                   if research else {}),
            }
            if research:
                budget = self.workspace.research_tools.control
                finalizing = (step >= step_limit - report_steps
                              or budget.searches >= budget.max_searches
                              or budget.passages >= budget.max_passages
                              or budget.failed_acquisitions >= budget.max_failed_acquisitions)
                if finalizing:
                    self.context.state["research_phase"] = "report"
                    self.context.append({"role": "user", "content":
                        "RESEARCH PHASE ENDED. Do not call more tools. Return final JSON NOW "
                        "with outcome, summary, evidence, limitations, artifacts, requested_content. "
                        "Use only observed evidence receipts; do not invent findings. "
                        "If evidence is insufficient, report partial or blocked."})
                else:
                    unread = self.workspace.research_tools.recommended_unread()
                    self.context.state["research_phase"] = (
                        "evaluate_candidate" if unread and budget.searches >= 2 else "collect"
                    )
            else:
                finalizing = False
            messages = self.context.prepare()
            tools = [] if finalizing else self.registry.catalog()
            request_size = {
                "messages_json_chars": len(json.dumps(messages)),
                "history_json_chars": len(json.dumps(self.context.history)),
                "tool_catalog_json_chars": len(json.dumps(tools)),
                "hud_chars": self.hud_chars,
                "compacted_exchanges": self.context.compacted,
            }
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
                    tools,
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
                "step": step + 1, **diagnostics, "request_size": request_size,
                "message_excerpt": raw[:16000], "message_truncated": len(raw) > 16000,
            })
            if finalizing and response.get("tool_calls"):
                # Do not execute new tool calls in the reserved synthesis phase.
                self.record("research_tool_calls_refused", {"step": step + 1,
                            "count": len(response["tool_calls"])})
                if step == step_limit - 1:
                    return self.research_fallback("Model continued requesting tools in reporting phase")
                continue
            if diagnostics.get("finish_reason") == "length":
                if finalizing:
                    self.record("research_final_truncated", {"step": step + 1})
                    if step == step_limit - 1:
                        return self.research_fallback("Model output was truncated during reporting")
                    continue
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
            if research and not calls:
                try:
                    value = json.loads(assistant["content"])
                    if (not isinstance(value, dict) or value.get("outcome") not in
                            {"completed", "partial", "blocked"}
                            or not isinstance(value.get("evidence"), list)
                            or not isinstance(value.get("limitations"), list)):
                        raise ValueError("Missing required research report fields")
                except (ValueError, TypeError):
                    self.record("research_invalid_final", {"step": step + 1})
                    if step == step_limit - 1:
                        return self.research_fallback("Model did not produce valid final JSON")
                    continue
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
                    "aios_work_history", "aios_read_work_result",
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
        if research:
            return self.research_fallback("Assignment model step budget reached")
        raise RuntimeError("Assignment model step budget reached")
