"""Workspace search/read pagination and event-driven command waiting contracts."""

import hashlib
import json
import sys
import threading
import time

import pytest

from aios_client.agent.context import RepositoryInstructions
from aios_client.tools.registry import ToolArguments
from aios_client.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    w = Workspace(root, tmp_path / "artifacts", ["private-credential"])
    yield w
    w.stop()


def pages(w, tool, args):
    cursor = None
    for _ in range(100):
        result = w.execute(tool, {**args, "cursor": cursor})
        yield result
        cursor = result["next_cursor"]
        if cursor is None:
            return
    pytest.fail("Pagination did not terminate")


def test_search_queries_regex_words_context_and_output_modes(workspace):
    w = workspace
    (w.root / "api.py").write_text(
        "# before\ndef send(payload):\n    resend(payload)\n    RECEIVE(payload)\n# after\n"
    )
    (w.root / "other.txt").write_text("send\n")
    args = {
        "query": [r"def\s+send", "receive"],
        "regex_mode": True,
        "case_sensitive": False,
        "pattern": "**/*.py",
        "context_lines": 1,
    }
    result = w.execute("search_files", args)
    assert [m["line"] for m in result["matches"]] == [2, 4]
    assert result["matches"][0]["context"][0] == {"line": 1, "text": "# before"}
    words = w.execute("search_files", {"query": "send", "whole_word": True})
    assert [(m["path"], m["line"]) for m in words["matches"]] == [
        ("api.py", 2),
        ("other.txt", 1),
    ]
    assert w.execute("search_files", {"query": "send", "mode": "files"})["files"] == [
        "api.py",
        "other.txt",
    ]
    counts = w.execute("search_files", {"query": ["send", "resend"], "mode": "count"})
    assert counts["counts"] == [
        {"path": "api.py", "count": 2},
        {"path": "other.txt", "count": 1},
    ]
    assert (
        counts["total_matches"] == 3
    )  # Matching lines, not occurrences or query hits.


def test_search_resumes_within_file_without_duplicate_lines(workspace):
    w = workspace
    (w.root / "many.py").write_text("hit\n" * 7)
    result = list(pages(w, "search_files", {"query": "hit", "limit": 2}))
    assert [m["line"] for p in result for m in p["matches"]] == list(range(1, 8))
    assert result[-1]["next_cursor"] is None
    token = result[0]["next_cursor"]
    with pytest.raises(ValueError, match="different search options"):
        w.execute("search_files", {"query": "other", "cursor": token})
    (w.root / "many.py").write_text("changed\n")
    with pytest.raises(ValueError, match="file changed"):
        w.execute("search_files", {"query": "hit", "cursor": token})
    with pytest.raises(ValueError, match="expired"):
        w.execute("search_files", {"query": "hit", "cursor": "invented"})


def test_find_and_search_reach_beyond_old_5000_file_ceiling(workspace):
    w = workspace
    for n in range(5002):
        (w.root / f"f{n:05}.py").write_text("needle\n" if n == 5001 else "")
    (w.root / ".env").write_text("needle\n")
    (w.root / "node_modules").mkdir()
    (w.root / "node_modules/hidden.py").write_text("needle\n")
    found = list(pages(w, "find_files", {"pattern": "*.py", "limit": 1000}))
    paths = [f for p in found for f in p["files"]]
    assert len(paths) == len(set(paths)) == 5002
    searched = list(pages(w, "search_files", {"query": "needle"}))
    assert searched[0]["matches"] == [] and searched[0]["next_cursor"]
    assert [m["path"] for p in searched for m in p["matches"]] == ["f05001.py"]
    assert searched[-1]["next_cursor"] is None


@pytest.mark.parametrize(
    "mode,key", [("matches", "matches"), ("files", "files"), ("count", "counts")]
)
def test_scan_budget_advances_even_when_pages_have_no_hits(workspace, mode, key):
    for name, text in [("a", "nothing"), ("b", "nothing"), ("c", "target")]:
        (workspace.root / name).write_text(text)
    result = list(
        pages(
            workspace,
            "search_files",
            {"query": "target", "mode": mode, "scan_limit": 1},
        )
    )
    assert result[0][key] == [] and result[0]["truncated"]
    assert sum(len(p[key]) for p in result) == 1


def test_search_skips_unreadable_and_oversized_files_and_bounds_regex(workspace):
    w = workspace
    (w.root / "binary").write_bytes(b"\xff")
    (w.root / "huge").write_text("x" * (1024 * 1024 + 1))
    assert w.execute("search_files", {"query": "x"})["skipped"] == 2
    with pytest.raises(ValueError, match="Invalid search regular expression"):
        w.execute("search_files", {"query": "[", "regex_mode": True})
    (w.root / "pathological").write_text("a" * 100000 + "!")
    started = time.monotonic()
    with pytest.raises(ValueError, match="time limit"):
        w.execute("search_files", {"query": "(a+)+$", "regex_mode": True})
    assert time.monotonic() - started < 3


def test_batched_reads_share_budgets_and_resume_every_line(workspace):
    w = workspace
    for name in ["a.py", "b.py"]:
        (w.root / name).write_text("".join(f"line {n}\n" for n in range(1, 206)))
    pending = [{"path": "a.py", "end": 205}, {"path": "b.py", "end": 205}]
    observed = {"a.py": [], "b.py": []}
    while pending:
        result = w.execute(
            "read_files", {"reads": pending, "max_lines": 83, "max_chars": 2000}
        )
        assert result["lines_read"] <= 83
        assert len(json.dumps(result["files"], ensure_ascii=True)) <= 2000
        for file in result["files"]:
            assert (
                file["sha256"]
                == hashlib.sha256((w.root / file["path"]).read_bytes()).hexdigest()
            )
            observed[file["path"]].extend(x["line"] for x in file["lines"])
        assert result["remaining"] != pending
        pending = result["remaining"]
    assert observed == {name: list(range(1, 206)) for name in observed}


def test_batched_read_errors_validation_redaction_and_nested_guidance(workspace):
    w = workspace
    (w.root / "src").mkdir()
    (w.root / "src/AGENTS.md").write_text("Use the nested test command.")
    (w.root / "src/code.py").write_text("private-credential\n")
    args = {"reads": [{"path": "src/code.py"}, {"path": "missing"}]}
    instructions = RepositoryInstructions(w)
    instructions.for_tool(args)
    assert "src/AGENTS.md" in instructions.loaded
    result = w.execute("read_files", args)
    assert result["files"][0]["lines"][0]["text"] == "[REDACTED]"
    assert result["files"][1]["error"] == "FileNotFoundError"
    with pytest.raises(ValueError, match="escapes workspace"):
        w.execute("read_files", {"reads": [{"path": "../outside"}]})
    for name, args in [
        ("read_files", {"reads": []}),
        ("read_files", {"reads": [{"path": "x", "start": True}]}),
        ("search_files", {"query": []}),
        ("wait_process", {"process_id": "x", "wait_seconds": 31}),
    ]:
        with pytest.raises(ToolArguments):
            w.execute(name, args)


def test_wait_process_output_cursor_completion_and_wait_timeout(workspace):
    w = workspace
    task = w.execute(
        "run_command",
        {
            "argv": [
                sys.executable,
                "-u",
                "-c",
                "import sys;print('ready',flush=True);sys.stdin.read();print('done')",
            ],
            "stdin": True,
        },
    )
    pid = task["process_id"]
    result = w.execute("wait_process", {"process_id": pid, "wait_seconds": 2})
    assert result["wait_reason"] == "output" and "ready" in result["output"]
    cursor = result["next_cursor"]
    timeout = w.execute(
        "wait_process", {"process_id": pid, "cursor": cursor, "wait_seconds": 0.02}
    )
    assert timeout["wait_reason"] == "wait_timeout"
    assert timeout["exit_code"] is None and not timeout["timed_out"]
    w.execute("write_process", {"process_id": pid, "close_stdin": True})
    assert w.processes[pid]["done"].wait(3)
    final = w.execute("wait_process", {"process_id": pid, "cursor": cursor})
    assert "done" in final["output"] and final["exit_code"] == 0
    end = w.execute("wait_process", {"process_id": pid, "cursor": final["next_cursor"]})
    assert end["wait_reason"] == "completed" and end["output_complete"]


def test_wait_process_wakes_on_workspace_cancellation(workspace):
    w = workspace
    pid = w.execute(
        "run_command",
        {"argv": [sys.executable, "-c", "import sys;sys.stdin.read()"], "stdin": True},
    )["process_id"]
    timer = threading.Timer(0.05, w.cancelled.set)
    timer.start()
    try:
        started = time.monotonic()
        result = w.execute("wait_process", {"process_id": pid, "wait_seconds": 10})
        assert result["wait_reason"] == "workspace_stopped"
        assert time.monotonic() - started < 1
    finally:
        timer.join()


def test_batched_reads_make_progress_with_long_lines_and_small_budget(workspace):
    (workspace.root / "long").write_text("x" * 2000 + "\nend\n")
    first = workspace.execute(
        "read_files", {"reads": [{"path": "long"}], "max_chars": 1000}
    )
    assert first["lines_read"] == 1 and first["files"][0]["lines"][0]["text_truncated"]
    second = workspace.execute(
        "read_files", {"reads": first["remaining"], "max_chars": 1000}
    )
    assert second["files"][0]["lines"][0]["text"] == "end"
    assert not second["remaining"]


def test_oversized_search_retains_continuation_metadata(workspace):
    (workspace.root / "big").write_text(("hit" + "x" * 297 + "\n") * 80)
    result = workspace.execute(
        "search_files", {"query": "hit", "limit": 40, "context_lines": 5}
    )
    assert result["artifact_id"] and result["next_cursor"]
    continued = workspace.execute(
        "search_files",
        {
            "query": "hit",
            "limit": 1,
            "context_lines": 5,
            "cursor": result["next_cursor"],
        },
    )
    assert continued["matches"][0]["line"] == 41
