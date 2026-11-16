"""Narrative context is bounded and cannot become an executable directive."""
from aios_client.agent.character_activity import normalize_activity, plan, review


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def completion(self, model, messages, tools, timeout=None):
        self.calls.append((messages, tools))
        return {"content": next(self.responses), "tool_calls": [],
                "_diagnostics": {"finish_reason": "stop"}}


class Gate:
    def acquire(self, timeout):
        return True
    def release(self):
        pass


def brief():
    return {"version": "character-activity-v1",
            "summary": "I want to build a small game",
            "intentions": ["Make the final move inevitable"],
            "open_questions": ["Is the rule valid?"]}


def test_narrative_plan_only_contains_structured_technical_steps():
    import time
    model = FakeModel(['{"steps":["Implement levels"],"design_decisions":["Keep one mechanic"],"unresolved":[]}'])
    value = plan(model, "test", Gate(), lambda: None, time.monotonic() + 30,
                 {"objective": "Implement a terminal game"}, brief())
    assert value["steps"] == ["Implement levels"]
    assert value["design_decisions"] == ["Keep one mechanic"]
    assert model.calls[0][1] == []
    assert "I want to build a small game" in model.calls[0][0][1]["content"]


def test_review_is_untrusted_and_does_not_change_checks():
    import time
    model = FakeModel(['{"summary":"I attempted the game.",'
                       '"reflection":"I need to verify the ending.",'
                       '"open_threads":["Review mechanics"]}'])
    evidence = {"status": "failed", "checks": [{"id": "test", "exit_code": 1}]}
    value = review(model, "test", Gate(), lambda: None, time.monotonic() + 30,
                   {"objective": "Make game"}, brief(), None, "worker said complete", evidence)
    assert "I attempted" in value["summary"]
    assert evidence["status"] == "failed"
    assert value["evidence_status"].startswith("worker_report")
    assert model.calls[0][1] == []


def test_invalid_brief_and_malformed_response_degrade_without_tools():
    import time
    model = FakeModel(["not json"])
    assert normalize_activity({"version": "other", "summary": "x"}) is None
    assert plan(model, "test", Gate(), lambda: None, time.monotonic() + 30,
                {"objective": "game"}, brief()) is None
    assert model.calls[0][1] == []
