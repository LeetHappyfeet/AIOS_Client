"""Operator-visible probes, compact process evidence and explicit result delivery."""

import asyncio
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from test_client import config
from test_v3 import Service, make_runner, model_api, tool

from aios_client.agent.context import RepositoryInstructions
from aios_client.assignment import assignment
from aios_client.config import ConfigStore
from aios_client.http import API
from aios_client.reporting import result_summary
from aios_client.tools.network import canonical_origin
from aios_client.tools.registry import ToolArguments
from aios_client.verification import snapshot, verify
from aios_client.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    w = Workspace(root, tmp_path / "logs", secrets=["PRIVATE-KEY"])
    yield w
    w.stop()


def completed(w, text, exit_code=0):
    task = w.execute(
        "run_command",
        {
            "argv": [
                sys.executable,
                "-c",
                f"import sys;sys.stdout.write({text!r});sys.exit({exit_code})",
            ]
        },
    )
    assert w.processes[task["process_id"]]["done"].wait(5)
    return task["process_id"]


def test_output_search_reads_later_errors_without_dumping_log(workspace):
    w = workspace
    text = "progress\n" * 1000 + "warning: first\nerror: PRIVATE-KEY\nfailed: last\n"
    pid = completed(w, text, 7)
    summary = w.execute("process_output_summary", {"process_id": pid})
    assert summary["exit_code"] == 7 and summary["output_complete"]
    assert summary["total_lines"] == 1003 and summary["counts_complete"]
    assert summary["matching_line_counts"] == {
        "error": 1,
        "warning": 1,
        "failed": 1,
        "traceback": 0,
    }
    assert len(json.dumps(summary)) < 3000
    page = w.execute(
        "search_process_output",
        {
            "process_id": pid,
            "query": "warning|error|failed",
            "regex_mode": True,
            "limit": 1,
        },
    )
    assert page["matches"][0]["line"] == 1001
    more = w.execute(
        "search_process_output",
        {"process_id": pid, "query": "error", "start_line": page["next_line"]},
    )
    assert more["matches"][0]["line"] == 1002
    assert "PRIVATE-KEY" not in json.dumps(more) and "[REDACTED]" in json.dumps(more)
    assert summary["snapshot_sha256"] == more["snapshot_sha256"]
    assert (
        "progress" in more["matches"][0]["context"][0]["text"]
        or more["matches"][0]["context"][0]["line"] == 1001
    )


def test_output_summary_never_calls_matching_words_a_failure(workspace):
    pid = completed(workspace, "0 errors; no warning\n")
    result = workspace.execute(
        "process_output_summary", {"process_id": pid, "head_lines": 0, "tail_lines": 0}
    )
    assert result["exit_code"] == 0 and result["head"] == result["tail"] == []
    assert result["matching_line_counts"]["error"] == 1
    assert "not a success/failure verdict" in result["interpretation"]
    workspace.capture_limit = 30
    pid = completed(workspace, "x" * 1000)
    result = workspace.execute("process_output_summary", {"process_id": pid})
    assert result["capture_truncated"] and result["snapshot_bytes"] == 30


def test_output_live_snapshot_and_regex_limits(workspace):
    w = workspace
    task = w.execute(
        "run_command",
        {
            "argv": [
                sys.executable,
                "-u",
                "-c",
                "import sys;print('ready');sys.stdin.read()",
            ],
            "stdin": True,
        },
    )
    pid = task["process_id"]
    w.execute("wait_process", {"process_id": pid, "wait_seconds": 3})
    result = w.execute("process_output_summary", {"process_id": pid})
    assert result["exit_code"] is None and not result["output_complete"]
    with pytest.raises(ValueError, match="Invalid output search"):
        w.execute(
            "search_process_output",
            {"process_id": pid, "query": "[", "regex_mode": True},
        )
    with pytest.raises(KeyError):
        w.execute(
            "search_process_output", {"process_id": "not-owned", "query": "ready"}
        )
    long_pid = w.execute(
        "run_command",
        {"argv": [sys.executable, "-c", "import sys;sys.stdout.write('a'*100000+'!')"]},
    )["process_id"]
    assert w.processes[long_pid]["done"].wait(5)
    with pytest.raises(ValueError, match="time limit"):
        w.execute(
            "search_process_output",
            {"process_id": long_pid, "query": "(a+)+$", "regex_mode": True},
        )


def test_inspect_paths_reports_metadata_without_reading_targets(workspace, tmp_path):
    w = workspace
    (w.root / "folder").mkdir()
    (w.root / "folder/file").write_bytes(b"hello")
    result = w.execute("inspect_path", {"path": "folder/file"})
    assert result["exists"] and result["kind"] == "file" and result["size_bytes"] == 5
    assert result["target_within_workspace"] and result["resolved_path"] == str(
        w.root / "folder/file"
    )
    assert w.execute("inspect_path", {"path": "folder"})["kind"] == "directory"
    assert w.execute("inspect_path", {"path": "missing"})["exists"] is False
    for path in ("../outside", "/etc/passwd", "C:\\data"):
        with pytest.raises(ValueError):
            w.execute("inspect_path", {"path": path})
    try:
        (w.root / "outside-link").symlink_to(tmp_path / "does-not-exist")
    except OSError:
        pytest.skip("Symlinks unavailable")
    instructions = RepositoryInstructions(w)
    instructions.for_tool({"path": "outside-link"}, "inspect_path")
    result = w.execute("inspect_path", {"path": "outside-link"})
    assert result["kind"] == "symlink" and result["exists"]
    assert not result["target_within_workspace"]
    with pytest.raises(ValueError):
        w.execute("read_file", {"path": "outside-link"})


def test_http_disabled_and_profile_policy_is_validated_before_network(
    workspace, tmp_path
):
    with pytest.raises(ValueError, match="not enabled"):
        workspace.execute("http_request", {"url": "http://127.0.0.1:1234/"})
    c = config(tmp_path)
    store = ConfigStore(tmp_path / "c.json")
    c["agents"]["worker"]["http_origins"] = [
        "http://127.0.0.1:8080",
        "https://EXAMPLE.COM:443",
    ]
    store.save(c)
    assert store.load() == c
    for origins in (
        "http://localhost",
        ["http://a.test/path"],
        ["http://u:p@a.test"],
        ["file:///tmp"],
        [True],
    ):
        c["agents"]["worker"]["http_origins"] = origins
        with pytest.raises(ValueError):
            store.save(c)
    assert canonical_origin("https://EXAMPLE.COM:443/health") == "https://example.com"
    assert canonical_origin("http://[::1]:8000/health") == "http://[::1]:8000"


def test_http_exact_origin_no_redirect_or_credentials_and_bounded_text(workspace):
    w = workspace
    calls = []

    def handler(request):
        calls.append(request)
        assert (
            "authorization" not in request.headers and "cookie" not in request.headers
        )
        if request.url.path == "/redirect":
            return httpx.Response(
                302,
                headers={"Location": "http://other.test/secret"},
                stream=httpx.ByteStream(b""),
            )
        return httpx.Response(
            503,
            headers={"Content-Type": "text/plain", "Set-Cookie": "hidden"},
            stream=httpx.ByteStream(b"PRIVATE-KEY\n" + b"x" * 10000),
        )

    w.http_tools.origins = frozenset(["http://app.test"])
    w.http_tools.transport = httpx.MockTransport(handler)
    result = w.execute(
        "http_request",
        {"url": "http://app.test/health", "max_bytes": 5000, "max_chars": 100},
    )
    assert result["ok"] and result["status_code"] == 503 and not result["http_success"]
    assert (
        result["body_truncated"]
        and result["excerpt_truncated"]
        and len(result["body"]) == 100
    )
    assert "PRIVATE-KEY" not in json.dumps(result)
    retained = (w.artifacts / result["artifact_id"]).read_text()
    assert "PRIVATE-KEY" not in retained and retained.startswith("[REDACTED]")
    assert "set-cookie" not in result["headers"]
    redirect = w.execute("http_request", {"url": "http://app.test/redirect"})
    assert (
        redirect["status_code"] == 302
        and not redirect["redirect_followed"]
        and len(calls) == 2
    )
    for url in (
        "http://app.test.evil/",
        "http://app.test:8080/",
        "https://app.test/",
        "http://u:p@app.test/",
        "http://app.test/?key=PRIVATE-KEY",
        "http://app.test/?key=PRIVATE%2DKEY",
    ):
        with pytest.raises(ValueError):
            w.execute("http_request", {"url": url})
    assert len(calls) == 2
    with pytest.raises(ToolArguments):
        w.execute("http_request", {"url": "http://app.test/", "method": "POST"})


def test_http_network_errors_binary_and_real_local_application(workspace):
    w = workspace
    w.http_tools.origins = frozenset(["http://app.test"])

    def fail(request):
        raise httpx.ConnectError("PRIVATE-KEY")

    w.http_tools.transport = httpx.MockTransport(fail)
    failed = w.execute("http_request", {"url": "http://app.test/"})
    assert failed["ok"] is False and failed["status_code"] is None
    assert failed["error"][
        "code"
    ] == "ConnectError" and "PRIVATE-KEY" not in json.dumps(failed)
    w.http_tools.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Type": "application/octet-stream"},
            stream=httpx.ByteStream(b"\xff\x00"),
        )
    )
    binary = w.execute("http_request", {"url": "http://app.test/"})
    assert (
        not binary["body_is_text"]
        and binary["body"] == ""
        and binary["artifact_id"] is None
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"healthy":true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        w.http_tools.origins = frozenset([origin])
        w.http_tools.transport = None
        result = w.execute("http_request", {"url": origin + "/health"})
        assert result["status_code"] == 200 and json.loads(result["body"])["healthy"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_publish_result_selects_real_content_and_matches_final_snapshot(workspace):
    w = workspace
    spec = assignment("Return a file excerpt")
    (w.root / "notes.md").write_bytes(b"# Notes\r\nPRIVATE-KEY\r\nThird line\r\n")
    baseline = snapshot(w)
    receipt = w.execute(
        "publish_result",
        {
            "outcome": "completed",
            "summary": "Read the notes",
            "files": [{"path": "notes.md", "start": 2, "end": 3}],
        },
    )
    assert receipt["staged"] and receipt["delivery"] == "final_report_after_host_checks"
    verify(w, spec, baseline, threading.Event(), time.monotonic() + 10)
    result = result_summary(w, spec, "Unstructured final prose")
    section = result["requested_content"][0]
    assert (
        result["source"] == "publish_result"
        and section["text"] == "[REDACTED]\r\nThird line\r\n"
    )
    assert section["matches_final_snapshot"] is True
    assert (
        section["source_sha256"]
        == hashlib.sha256((w.root / "notes.md").read_bytes()).hexdigest()
    )
    (w.root / "notes.md").write_text("overwritten")
    verify(w, spec, baseline, threading.Event(), time.monotonic() + 10)
    assert (
        result_summary(w, spec, "Done")["requested_content"][0][
            "matches_final_snapshot"
        ]
        is False
    )


def test_publish_is_atomic_and_excludes_secrets_paths(workspace):
    w = workspace
    args = {
        "outcome": "partial",
        "summary": "Useful findings",
        "content": [{"text": "Observed PRIVATE-KEY", "label": "Answer"}],
    }
    w.execute("publish_result", args)
    previous = w.published_result
    for path in (".env", ".git/config", "../outside", "missing"):
        with pytest.raises((ValueError, OSError)):
            w.execute(
                "publish_result",
                {
                    "outcome": "completed",
                    "summary": "Replace",
                    "files": [{"path": path}],
                },
            )
        assert w.published_result == previous
    result = result_summary(w, assignment("Inspect"), "Done", execution_failed=True)
    assert (
        result["outcome"] == "partial"
        and result["requested_content"][0]["source_kind"] == "worker_text"
    )
    assert "PRIVATE-KEY" not in json.dumps(result)
    assert "verification is unavailable" in result["limitations"][0]


def test_publish_result_survives_session_and_is_sent_once_with_host_evidence(
    tmp_path, monkeypatch
):
    r = make_runner(tmp_path, monkeypatch)
    spec = assignment("Return the actual README")
    service = Service(spec)
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    from pathlib import Path

    Path(r.profile["workspace"]).joinpath("README.md").write_text("# Actual README\n")
    turns = 0

    def model(body):
        nonlocal turns
        turns += 1
        if turns == 1:
            names = {t["function"]["name"] for t in body["tools"]}
            assert {
                "publish_result",
                "inspect_path",
                "http_request",
                "search_process_output",
                "process_output_summary",
            } <= names
            return tool(
                "publish_result",
                {
                    "outcome": "completed",
                    "summary": "Read the README",
                    "files": [{"path": "README.md"}],
                },
            )
        assert json.loads(body["messages"][-1]["content"])["staged"]
        return {"role": "assistant", "content": "Selected README content for return."}

    r.model = model_api(model)
    try:
        result = r.work()
        assert result["status"] == "finished"
        summary = service.bodies("/finish")[0]["result"]["result_summary"]
        assert summary["requested_content"][0]["text"] == "# Actual README\n"
        assert summary["requested_content"][0]["matches_final_snapshot"] is True
        assert len(service.bodies("/observe")) == 2 and turns == 2
        assert (
            r.journal.state(service.rid)["published_result"]["summary"]
            == "Read the README"
        )
        assert (
            service.bodies("/finish")[0]["result"]["task_outcome"]
            == "not_independently_verified"
        )
    finally:
        r.close()


def test_http_byte_cap_cannot_retain_a_known_credential_prefix(workspace):
    w = workspace
    w.http_tools.origins = frozenset(["http://app.test"])
    w.http_tools.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            stream=httpx.ByteStream(b"PRIVATE-KEY trailing"),
        )
    )
    result = w.execute("http_request", {"url": "http://app.test/", "max_bytes": 6})
    assert result["body_truncated"] and result["body"] == "[REDACTED_PARTIAL]"
    assert (w.artifacts / result["artifact_id"]).read_text() == "[REDACTED_PARTIAL]"


def test_publish_failure_returns_partial_content_without_claiming_verification(
    tmp_path, monkeypatch
):
    r = make_runner(tmp_path, monkeypatch)
    service = Service(assignment("Inspect and return partial findings"))
    r.api = API("http://aios", transport=httpx.MockTransport(service.handle))
    turns = 0

    def model(body):
        nonlocal turns
        turns += 1
        if turns == 1:
            return tool(
                "publish_result",
                {
                    "outcome": "completed",
                    "summary": "Found useful evidence",
                    "content": [{"text": "An application setting was missing"}],
                },
            )
        return tool(
            "task_blocked", {"reason": "Need the application configuration"}, "blocked"
        )

    r.model = model_api(model)
    try:
        result = r.work()
        assert result["status"] == "interrupted"
        final = service.bodies("/finish")[0]["result"]
        assert final["verification"] == "unverified"
        assert final["result_summary"]["outcome"] == "partial"
        assert (
            final["result_summary"]["requested_content"][0]["text"]
            == "An application setting was missing"
        )
    finally:
        r.close()


@pytest.mark.parametrize("delay_headers", [True, False])
def test_http_aggregate_deadline_covers_headers_and_trickled_body(
    workspace, delay_headers
):
    w = workspace
    w.http_tools.origins = frozenset(["http://app.test"])

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.01)
                yield b"x"

    async def handler(request):
        if delay_headers:
            await asyncio.sleep(1)
        return httpx.Response(200, stream=SlowBody())

    w.http_tools.transport = httpx.MockTransport(handler)
    started = time.monotonic()
    result = w.execute(
        "http_request", {"url": "http://app.test/", "timeout_seconds": 0.05}
    )
    assert not result["ok"] and result["error"]["code"] == "TimeoutError"
    assert result["status_code"] == (None if delay_headers else 200)
    assert time.monotonic() - started < 1


def test_http_unexpected_compression_is_not_expanded(workspace):
    w = workspace
    w.http_tools.origins = frozenset(["http://app.test"])
    w.http_tools.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
            stream=httpx.ByteStream(b"not-expanded-or-decoded"),
        )
    )
    result = w.execute("http_request", {"url": "http://app.test/"})
    assert result["ok"] and result["body_encoded"] and not result["body_is_text"]
    assert result["artifact_id"] is None
