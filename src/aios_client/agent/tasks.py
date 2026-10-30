"""Local plan and blocked-state tools; neither changes assignment authority."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class TaskBlocked(RuntimeError):
    pass


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    status: Literal["pending", "in_progress", "completed", "blocked"] = "pending"


class TaskTools:
    def __init__(self, record, progress, stop=lambda: None):
        self.plan, self.explanation, self.blocked = [], "", None
        self.record, self.progress, self.stop = record, progress, stop

    def update_plan(
        self,
        steps: Annotated[list[PlanStep], Field(max_length=30)],
        explanation: Annotated[str, Field(max_length=1000)] = "",
    ):
        if len({step["id"] for step in steps}) != len(steps):
            raise ValueError("Plan step IDs must be unique")
        if sum(step["status"] == "in_progress" for step in steps) > 1:
            raise ValueError("Only one plan step can be in progress")
        self.plan, self.explanation = steps, explanation
        result = {"plan": self.plan, "explanation": explanation}
        self.record("plan", result)
        return result

    def task_blocked(
        self, reason: Annotated[str, Field(min_length=1, max_length=1000)]
    ):
        if not reason.strip():
            raise ValueError("Blocked reason must not be blank")
        self.blocked = reason
        self.record("task_blocked", {"reason": reason})
        self.stop()
        try:
            self.progress("blocked", reason)
        except RuntimeError:
            self.record(
                "delivery_pending",
                {"message": "Blocked milestone retained for delivery"},
            )
        return {"task_state": "blocked", "reason": reason, "requires_operator": True}

    def register(self, registry):
        registry.add(
            "update_plan",
            self.update_plan,
            "Maintain local work steps; plan completion is not verification",
            "agent.plan",
        )
        registry.add(
            "task_blocked",
            self.task_blocked,
            "Stop this execution and report a missing prerequisite or clarification",
            "agent.control",
        )
