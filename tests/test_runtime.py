"""Real filesystem/process contracts for the coding runtime and future OS adapter seam."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

from aios_client.agent.context import (
    ContextBudget,
    RepositoryInstructions,
    WorkingContext,
)
from aios_client.agent.session import AgentSession
from aios_client.agent.tasks import TaskBlocked
from aios_client.assignment import assignment
from aios_client.journal import Journal
from aios_client.tools.registry import ToolArguments
from aios_client.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace with spaces"
    root.mkdir()
    w = Workspace(root, tmp_path / "artifacts", ["test-private-key"])
    yield w
    w.stop()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(w, argv, **kwargs):
    result = w.execute("run_command", {"argv": argv, **kwargs})
    assert result["ok"] and result["status"] == "started"
    return result["process_id"]


def done(w, pid):
    assert w.processes[pid]["done"].wait(5)
    return w.execute("read_process", {"process_id": pid})


def test_registry_catalog_drives_strict_validation(workspace):
    w = workspace
    schemas = {
        x["function"]["name"]: x["function"]["parameters"] for x in w.registry.catalog()
    }
    assert schemas["read_file"]["additionalProperties"] is False
    assert schemas["read_file"]["required"] == ["path"]
    assert {"command", "argv", "shell", "stdin", "timeout_seconds"} <= schemas[
        "run_command"
    ]["properties"].keys()
    assert "Edit" in schemas["apply_edits"]["$defs"]
    for name, args in [
        ("read_file", {"path": 4}),
        ("read_file", {"path": "a", "start": True}),
        ("read_file", {"path": "a", "surprise": "test-private-key"}),
        ("missing", {}),
        ("apply_edits", {"edits": []}),
        ("run_command", {"argv": ["python"], "stdin": "yes"}),
    ]:
        with pytest.raises(ToolArguments) as error:
            w.execute(name, args)
        assert "test-private-key" not in str(error.value)


def test_batch_edits_preflight_crlf_moves_and_deletion(workspace):
    w = workspace
    a, b = w.root / "a.py", w.root / "b.py"
    a.write_bytes(b"x = 1\r\n")
    b.write_text("unchanged", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        w.execute(
            "apply_edits",
            {
                "edits": [
                    {
                        "path": "a.py",
                        "expected_hash": digest(a),
                        "old": "1",
                        "new": "2",
                    },
                    {"path": "b.py", "expected_hash": "stale", "operation": "delete"},
                ]
            },
        )
    assert a.read_bytes() == b"x = 1\r\n" and b.exists()
    result = w.execute(
        "apply_edits",
        {
            "edits": [
                {"path": "a.py", "expected_hash": digest(a), "old": "1", "new": "2"},
                {
                    "path": "b.py",
                    "expected_hash": digest(b),
                    "operation": "move",
                    "destination": "nested/moved.py",
                },
                {
                    "path": "new.py",
                    "expected_hash": "",
                    "operation": "create",
                    "new": "new\n",
                },
            ]
        },
    )
    assert result["ok"] and len(result["files"]) == 4
    assert a.read_bytes() == b"x = 2\r\n"
    assert not b.exists() and (w.root / "nested/moved.py").read_text() == "unchanged"
    w.execute(
        "delete_file", {"path": "new.py", "expected_hash": digest(w.root / "new.py")}
    )
    assert not (w.root / "new.py").exists()


def test_batch_restores_published_files_on_error(workspace, monkeypatch):
    w = workspace
    (w.root / "a").write_text("one")
    (w.root / "b").write_text("two")
    original = w.filesystem._replace
    count = 0

    def fail_second(path, data, mode):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected disk failure")
        original(path, data, mode)

    monkeypatch.setattr(w.filesystem, "_replace", fail_second)
    with pytest.raises(OSError):
        w.execute(
            "apply_edits",
            {
                "edits": [
                    {
                        "path": n,
                        "expected_hash": digest(w.root / n),
                        "old": old,
                        "new": "changed",
                    }
                    for n, old in [("a", "one"), ("b", "two")]
                ]
            },
        )
    assert (w.root / "a").read_text() == "one"
    assert (w.root / "b").read_text() == "two"
    assert not list(w.root.glob(".aios-edit-*"))


def test_batch_rejects_overlaps_symlinks_and_git_metadata(workspace):
    w = workspace
    (w.root / "a").write_text("a")
    bad = [
        [
            {
                "path": "a",
                "expected_hash": digest(w.root / "a"),
                "operation": "move",
                "destination": "a",
            }
        ],
        [
            {
                "path": "../outside",
                "expected_hash": "",
                "operation": "create",
                "new": "wrong",
            }
        ],
        [
            {
                "path": ".git/config",
                "expected_hash": "",
                "operation": "create",
                "new": "wrong",
            }
        ],
        [
            {"path": "a", "expected_hash": digest(w.root / "a"), "operation": "delete"},
            {"path": "a", "expected_hash": "", "operation": "create", "new": "wrong"},
        ],
    ]
    for edits in bad:
        with pytest.raises(ValueError):
            w.execute("apply_edits", {"edits": edits})
    try:
        (w.root / "alias").symlink_to(w.root / "a")
    except OSError:
        return  # Windows may not grant symlink creation to this test account.
    with pytest.raises(ValueError, match="Symlink"):
        w.execute("read_file", {"path": "alias"})


def test_glob_search_and_retained_artifact_paging(workspace):
    w = workspace
    (w.root / "a.py").write_text("Hello\nhello\n")
    (w.root / "b.txt").write_text("HELLO\n")
    (w.root / ".env").write_text("HELLO=private")
    assert w.execute("find_files", {"pattern": "**/*.py"})["files"] == ["a.py"]
    matches = w.execute(
        "search_files", {"query": "HELLO", "case_sensitive": False, "pattern": "*.py"}
    )
    assert len(matches["matches"]) == 2
    large = w.bound_result({"payload": "test-private-key" * 4000})
    artifact = large["artifact_id"]
    page = w.execute("read_artifact", {"artifact_id": artifact, "limit": 100})
    assert page["next_cursor"] == 100 and not page["complete"]
    assert "test-private-key" not in (w.artifacts / artifact).read_text()
    with pytest.raises(ValueError):
        w.execute("read_artifact", {"artifact_id": "../a.py"})


def test_argv_stdin_eof_and_process_receipts(workspace):
    w = workspace
    events = []
    w.process_manager.on_event = lambda kind, body: events.append((kind, body))
    marker = "a space; $(echo surprise) & literal"
    pid = run(
        w,
        [
            sys.executable,
            "-u",
            "-c",
            "import sys;print(sys.argv[1]);print(sys.stdin.read())",
            marker,
        ],
        stdin=True,
    )
    queued = w.execute(
        "write_process",
        {"process_id": pid, "text": "hello test-private-key\n", "close_stdin": True},
    )
    assert queued["delivery"] == "queued"
    result = done(w, pid)
    assert result["exit_code"] == 0 and marker in result["output"]
    assert "hello [REDACTED]" in result["output"]
    assert result["stdin_closed"] and not result["input_pending"]
    assert [x[0] for x in events if x[0] != "process_output"] == ["process_started", "process_exited"]
    assert any(kind == "process_output" for kind, body in events)
    assert events[0][1]["os_pid"] == w.processes[pid]["p"].pid
    assert "test-private-key" not in json.dumps(events)
    with pytest.raises(ValueError):
        w.execute("write_process", {"process_id": pid, "text": "too late"})


def test_default_stdin_is_closed_and_shell_availability_is_explicit(workspace):
    w = workspace
    pid = run(w, [sys.executable, "-c", "import sys;print(repr(sys.stdin.read()))"])
    assert done(w, pid)["output"].strip() == "''"
    info = w.execute("host_info", {})
    assert info["scope"] == "workspace" and info["stdin_pipe"] and not info["pty"]
    assert info["default_shell"] in ("sh", "cmd")
    assert "workspace.files" in info["tool_capabilities"]
    unavailable = "cmd" if os.name != "nt" else "bash"
    with pytest.raises(ValueError, match="unavailable"):
        w.execute("run_command", {"command": "echo wrong", "shell": unavailable})
    for args in (
        {"command": "echo no", "argv": ["echo", "no"]},
        {},
        {"argv": ["echo"], "shell": "bash"},
    ):
        with pytest.raises(ValueError):
            w.execute("run_command", args)


def test_pipe_input_does_not_block_controller_and_cancel_stops_process(workspace):
    w = workspace
    pid = run(w, [sys.executable, "-c", "import time;time.sleep(30)"], stdin=True)
    started = time.monotonic()
    for _ in range(8):
        w.execute("write_process", {"process_id": pid, "text": "x" * 8000})
    assert time.monotonic() - started < 2
    w.stop()
    assert w.processes[pid]["p"].poll() is not None
    assert w.processes[pid]["done"].wait(3)
    with pytest.raises(RuntimeError):
        w.execute("run_command", {"argv": [sys.executable, "-c", "print(1)"]})


def test_process_timeout_cursor_and_git_paths(workspace):
    w = workspace
    pid = run(
        w, [sys.executable, "-c", "import time;time.sleep(30)"], timeout_seconds=0.1
    )
    assert done(w, pid)["timed_out"]
    subprocess.run(["git", "init", "-q", str(w.root)], check=True)
    (w.root / "odd file.py").write_text("first\n")
    subprocess.run(["git", "-C", str(w.root), "add", "odd file.py"], check=True)
    diff = w.execute("git_diff", {"staged": True, "path": "odd file.py"})
    assert "+first" in diff["diff"]
    with pytest.raises(ValueError):
        w.execute("git_show", {"revision": "--output=evil"})


def test_instruction_scopes_and_pinned_assignment_survive_compaction(workspace):
    w = workspace
    (w.root / "AGENTS.md").write_text("Use the root test command.")
    (w.root / "src").mkdir()
    (w.root / "src/AGENTS.md").write_text("Preserve the public API in src.")
    instructions = RepositoryInstructions(w)
    instructions.for_tool({"path": "src/code.py"})
    assert set(instructions.loaded) == {"AGENTS.md", "src/AGENTS.md"}
    spec = assignment(
        {"objective": "Keep the original objective", "preserve_paths": ["test.py"]}
    ).model_dump(mode="json")
    context = WorkingContext("SYSTEM", spec, 5000, instructions)
    for n in range(6):
        args = "not json" if n == 0 else json.dumps({"path": "src/code.py"})
        context.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": str(n), "function": {"name": "read_file", "arguments": args}}
                ],
            }
        )
        context.append(
            {
                "role": "tool",
                "tool_call_id": str(n),
                "content": json.dumps(
                    {
                        "ok": n != 0,
                        "output": "x" * 3000,
                        **(
                            {"error": {"code": "invalid", "message": "must retain"}}
                            if n == 0
                            else {}
                        ),
                    }
                ),
            }
        )
    messages = context.prepare()
    assert len(json.dumps(messages)) <= 5000 and context.compacted > 0
    assert json.loads(messages[1]["content"]) == spec
    assert (
        "must retain" in messages[2]["content"]
        and "Preserve the public API" in messages[2]["content"]
    )
    for index, message in enumerate(messages):
        if message["role"] == "tool":
            assert any(
                x.get("tool_calls")
                and message["tool_call_id"] in [c["id"] for c in x["tool_calls"]]
                for x in messages[:index]
            )
    context.budget = 100
    with pytest.raises(ContextBudget, match="Pinned"):
        context.prepare()


class ScriptedModel:
    def __init__(self, script):
        self.script = script
        self.http = type("HTTP", (), {"timeout": type("Timeout", (), {"read": 2})()})()

    def completion(self, model_id, messages, tools, timeout=None):
        return self.script(messages, tools)


def call(name, arguments, ident=None):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": ident or str(uuid4()),
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def make_session(w, tmp_path, model, spec=None):
    journal = Journal(tmp_path / "journal.db")
    journal.start("run", "worker", "task")
    spec = spec or assignment("Do bounded coding work")
    session = AgentSession(
        model=model,
        model_id="fixture",
        workspace=w,
        spec=spec,
        system="SYSTEM",
        journal=journal,
        run_id="run",
        control=lambda: None,
        gate=threading.BoundedSemaphore(1),
        deadline=time.monotonic() + 10,
        progress=lambda *args: None,
        observe=lambda *args: None,
    )
    return session, journal


def test_session_plan_blocked_stops_later_tools_and_persists_state(workspace, tmp_path):
    step = 0

    def model(messages, tools):
        nonlocal step
        step += 1
        if step == 1:
            return call(
                "update_plan",
                {
                    "steps": [
                        {
                            "id": "inspect",
                            "description": "Inspect configuration",
                            "status": "in_progress",
                        }
                    ]
                },
            )
        assert "Inspect configuration" in messages[2]["content"]
        one = call("task_blocked", {"reason": "Need the service name"})
        one["tool_calls"] += call("run_command", {"command": "echo wrong > unwanted"})[
            "tool_calls"
        ]
        return one

    session, journal = make_session(workspace, tmp_path, ScriptedModel(model))
    try:
        with pytest.raises(TaskBlocked, match="service name"):
            session.run()
        state = journal.state("run")
        assert state["plan"][0]["status"] == "in_progress"
        assert state["blocked_reason"] == "Need the service name"
        assert not (workspace.root / "unwanted").exists()
        journal.db.close()
        journal = Journal(tmp_path / "journal.db")
        journal.recover()
        assert journal.state("run")["blocked_reason"] == "Need the service name"
        assert journal.status()[0]["status"] == "interrupted"
    finally:
        journal.db.close()


def test_session_repeated_tool_id_cannot_repeat_side_effect(workspace, tmp_path):
    def model(messages, tools):
        return call(
            "apply_patch",
            {"path": "new", "expected_hash": "", "old": "", "new": "once"},
            "same",
        )

    session, journal = make_session(workspace, tmp_path, ScriptedModel(model))
    try:
        with pytest.raises(ValueError, match="repeated"):
            session.run()
        assert (workspace.root / "new").read_text() == "once"
        assert (
            len([x for x in journal.events("run") if x["kind"] == "tool_started"]) == 1
        )
    finally:
        journal.db.close()


def test_session_uses_nested_guidance_before_edit_and_keeps_host_verification_separate(
    workspace, tmp_path
):
    w = workspace
    (w.root / "src").mkdir()
    (w.root / "src/AGENTS.md").write_text("Use exact replacements in src.")
    (w.root / "src/calc.py").write_text("value = 1\n")
    step = 0

    def model(messages, tools):
        nonlocal step
        step += 1
        if step == 1:
            return call("read_file", {"path": "src/calc.py"})
        if step == 2:
            assert "Use exact replacements" in messages[2]["content"]
            h = json.loads(messages[-1]["content"])["sha256"]
            return call(
                "apply_patch",
                {"path": "src/calc.py", "expected_hash": h, "old": "1", "new": "2"},
            )
        return {
            "role": "assistant",
            "content": "Changed the value; host checks still determine verification.",
        }

    session, journal = make_session(w, tmp_path, ScriptedModel(model))
    try:
        assert "host checks" in session.run()
        assert (w.root / "src/calc.py").read_text() == "value = 2\n"
        assert journal.status()[0]["status"] == "running"
        assert journal.state("run")["assignment_hash"] == session.spec.digest()
    finally:
        journal.db.close()


def test_direct_nested_edit_waits_for_model_to_receive_guidance(workspace, tmp_path):
    w = workspace
    (w.root / "nested").mkdir()
    (w.root / "nested/AGENTS.md").write_text(
        "Follow the nested format before changing code."
    )
    step = 0

    def model(messages, tools):
        nonlocal step
        step += 1
        if step == 2:
            assert not (w.root / "nested/new.py").exists()
            assert "Follow the nested format" in messages[2]["content"]
            assert not json.loads(messages[-1]["content"])["ok"]
        if step <= 2:
            return call(
                "apply_patch",
                {
                    "path": "nested/new.py",
                    "expected_hash": "",
                    "old": "",
                    "new": "value = 1\n",
                },
            )
        return {"role": "assistant", "content": "Edited after reading nested guidance."}

    session, journal = make_session(w, tmp_path, ScriptedModel(model))
    try:
        session.run()
        assert (w.root / "nested/new.py").read_text() == "value = 1\n"
    finally:
        journal.db.close()


def test_blocked_session_keeps_state_when_transport_is_unavailable(workspace, tmp_path):
    session, journal = make_session(
        workspace,
        tmp_path,
        ScriptedModel(
            lambda m, t: call("task_blocked", {"reason": "Missing configuration"})
        ),
    )

    def offline(*args):
        raise RuntimeError("offline")

    session.tasks.progress = offline
    session.observe = offline
    try:
        with pytest.raises(TaskBlocked):
            session.run()
        assert journal.state("run")["blocked_reason"] == "Missing configuration"
        assert (
            journal.status()[0]["session"]["blocked_reason"] == "Missing configuration"
        )
    finally:
        journal.db.close()


def test_aios_runner_executes_expanded_coding_tools_then_host_checks(
    tmp_path, monkeypatch
):
    import httpx
    from test_v3 import Service, make_runner, model_api, tool

    from aios_client.http import API

    r = make_runner(tmp_path, monkeypatch)
    spec = assignment(
        {
            "objective": "Create valid Python and inspect command input",
            "required_checks": [
                {
                    "id": "syntax",
                    "command": f'"{sys.executable}" -m compileall -q calc.py',
                }
            ],
            "expected_outputs": [{"path": "calc.py"}],
        }
    )
    service = Service(spec)
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    phase = 0
    process_id = None

    def model(body):
        nonlocal phase, process_id
        messages = body["messages"]
        assert json.loads(messages[1]["content"]) == spec.model_dump(mode="json")
        names = {t["function"]["name"] for t in body["tools"]}
        assert {
            "apply_edits",
            "write_process",
            "host_info",
            "update_plan",
            "aios_recall",
        } <= names
        phase += 1
        if phase == 1:
            return tool(
                "update_plan",
                {
                    "steps": [
                        {
                            "id": "code",
                            "description": "Create and inspect code",
                            "status": "in_progress",
                        }
                    ]
                },
            )
        if phase == 2:
            return tool(
                "apply_edits",
                {
                    "edits": [
                        {
                            "path": "calc.py",
                            "expected_hash": "",
                            "operation": "create",
                            "new": "value = 2\n",
                        },
                        {
                            "path": "notes.txt",
                            "expected_hash": "",
                            "operation": "create",
                            "new": "retained notes\n",
                        },
                    ]
                },
                "edit",
            )
        if phase == 3:
            return tool(
                "run_command",
                {
                    "argv": [
                        sys.executable,
                        "-u",
                        "-c",
                        "import sys;print(sys.stdin.read().upper())",
                    ],
                    "stdin": True,
                },
                "process",
            )
        if phase == 4:
            process_id = json.loads(messages[-1]["content"])["process_id"]
            return tool(
                "write_process",
                {
                    "process_id": process_id,
                    "text": "actual input\n",
                    "close_stdin": True,
                },
                "stdin",
            )
        if messages[-1]["role"] == "tool":
            result = json.loads(messages[-1]["content"])
            if result.get("output_complete"):
                assert result["exit_code"] == 0 and "ACTUAL INPUT" in result["output"]
                return tool(
                    "update_plan",
                    {
                        "steps": [
                            {
                                "id": "code",
                                "description": "Create and inspect code",
                                "status": "completed",
                            }
                        ]
                    },
                    "plan-done",
                )
            if result.get("plan"):
                return {
                    "role": "assistant",
                    "content": "Created code and observed real process output.",
                }
        time.sleep(0.02)
        return tool("read_process", {"process_id": process_id}, str(uuid4()))

    r.model = model_api(model)
    try:
        result = r.work()
        assert result["status"] == "finished"
        assert result["result"]["evidence"]["status"] == "passed"
        assert result["session"]["plan"][0]["status"] == "completed"
        assert (
            service.bodies("/finish")[0]["result"]["evidence"]["checks"][0]["exit_code"]
            == 0
        )
        assert r.journal.state(service.rid)["assignment_hash"] == spec.digest()
    finally:
        r.close()


def test_aios_blocked_tool_reports_interrupted_without_verification(
    tmp_path, monkeypatch
):
    import httpx
    from test_v3 import Service, make_runner, model_api, tool

    from aios_client.http import API

    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Inspect the unspecified service"))
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    r.model = model_api(
        lambda body: tool("task_blocked", {"reason": "Which service should I inspect?"})
    )
    try:
        result = r.work()
        assert result["status"] == "interrupted"
        assert result["result"]["task_state"] == "blocked"
        assert result["result"]["verification"] == "unverified"
        assert result["session"]["blocked_reason"] == "Which service should I inspect?"
        assert any(p["kind"] == "blocked" for p in service.bodies("/progress"))
        assert service.bodies("/finish")[0]["status"] == "interrupted"
        assert not r.stop_event.is_set()
    finally:
        r.close()


def test_injected_process_backend_drives_host_capabilities(tmp_path):
    from aios_client.tools.platforms import ProcessBackend

    class Backend(ProcessBackend):
        def capabilities(self):
            return {**super().capabilities(), "adapter": "fixture-pipes"}

        def launch(self, *args):
            self.launched = args
            return super().launch(*args)

    backend = Backend()
    root = tmp_path / "root"
    root.mkdir()
    w = Workspace(root, tmp_path / "logs", process_backend=backend)
    try:
        assert w.host_info()["adapter"] == "fixture-pipes"
        pid = run(w, [sys.executable, "-c", "print(42)"])
        assert done(w, pid)["exit_code"] == 0
        assert backend.launched[1][0] == sys.executable
        assert backend.launched[3] == root
    finally:
        w.stop()


def test_session_compacts_real_observations_and_checkpoints_seen_calls(
    workspace, tmp_path
):
    (workspace.root / "big.txt").write_text(
        "a" * 900 + "\n" + "b" * 900 + "\n" + "c" * 900 + "\n"
    )
    step = 0

    def model(messages, tools):
        nonlocal step
        step += 1
        assert len(json.dumps(messages)) <= 5000
        if step <= 8:
            return call("read_file", {"path": "big.txt"}, f"read-{step}")
        return {"role": "assistant", "content": "Inspected the retained file."}

    session, journal = make_session(
        workspace,
        tmp_path,
        ScriptedModel(model),
        assignment({"objective": "Inspect files", "budgets": {"context_chars": 5000}}),
    )
    try:
        session.run()
        state = journal.state("run")
        assert state["context"]["compacted_exchanges"] > 0
        assert state["seen_calls"] == [f"read-{n}" for n in range(1, 9)]
        assert (
            journal.db.execute(
                "SELECT count(*) FROM events WHERE kind='tool_result'"
            ).fetchone()[0]
            == 8
        )
    finally:
        journal.db.close()


def test_large_recall_keeps_status_revision_and_full_provenance(workspace):
    result = workspace.bound_result(
        {
            "status": "changed",
            "knowledge_revision": 42,
            "hits": [
                {"provenance": "large source " * 3000, "durable_knowledge": False}
            ],
        }
    )
    assert result["status"] == "changed" and result["knowledge_revision"] == 42
    retained = json.loads((workspace.artifacts / result["artifact_id"]).read_text())
    assert retained["hits"][0]["durable_knowledge"] is False

