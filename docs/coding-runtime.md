# Coding runtime and the next system-work pass

## Run diagnostics and verification scope

Plain workspaces are supported. Git inspection detects a non-repository once per
run and returns `is_repository: false`, `status: skipped`, and `head: null`.
Missing Git is reported separately from repository absence; unexpected probe
failures remain failures. An initialized repository without commits has
`head_status: unborn`. Error output is never stored as a commit identifier.

The run lifecycle status `finished` means execution ended. `evidence.status`
describes only the immutable assignment's configured checks, output expectations,
and workspace snapshot. Results and final observations explicitly label
`task_outcome: not_independently_verified`. For example, a JSON structure check
that permits `outcome: blocked` may pass while the requested work is blocked.
Use task-specific acceptance checks to verify the behavior you actually require.
The snapshot names its root and exclusions; it does not observe writes elsewhere
on the host or establish network isolation.

Shell checks must preserve the failure status of the operation under test. Prefer
an argv script with assertions, or explicit error propagation. A trailing
successful `echo`, cleanup command, or pipeline can hide an earlier failure.
The model instructions explain this; the host preserves assigned command text
and does not rewrite arbitrary shell programs or treat their exit status as
proof of every intermediate step.

Local activity logs include absolute resolved working directories and concise
tool receipts (paths, edit hashes, process status, artifact IDs). Terminal newlines
no longer create artificial empty records. Full process output and session traces
remain local, with existing redaction and retention behavior.

Each completed run logs a durable `delivery_summary`: unique report counts by
kind (`observe`, `progress`, `finish`), attempts, failed attempts, acknowledged,
pending, rejected and discarded reports, and serialized payload bytes. Retries
retain stable delivery IDs and counters across restarts. Byte counts cover JSON
request bodies, exclude headers, and count each attempt separately. An API
acknowledgement does not prove downstream memory ingestion. These counters cover
report notifications, not lease renewals, context/recall requests, or model traffic.
The summary is local and does not generate an additional AIOS memory observation.

AIOS_Client owns the local model/tool loop. AIOS provides the immutable assignment,
identity/recall context, lease, cancellation, and result endpoints. It does not need
to interpret file edits, terminal output, or the model's intermediate coding steps.
This pass changes only the client; it builds on the v3 worker integration.

## Runtime structure

| Module | Responsibility |
|---|---|
| `runner.py` | AIOS negotiation, claims, lease control, observations, progress, final host verification and delivery |
| `agent/session.py` | Provider-native model/tool iteration, cancellation checks, tool-call identity, local checkpoints |
| `agent/context.py` | Immutable assignment, repository guidance, working history, bounded receipt compaction |
| `agent/tasks.py` | Local plans and explicit blocked state |
| `tools/registry.py` | Generate schemas from typed handlers; strictly validate arguments and dispatch tools |
| `tools/files.py` | Paginated discovery/search, bounded single/batch reads and hash-checked edits |
| `tools/output.py` | Deterministic searches, head/tail excerpts and term counts from retained command logs |
| `tools/network.py` | Read-only HTTP probes to operator-configured exact origins |
| `tools/results.py` | Explicit findings and file-excerpt selection for the final result |
| `tools/processes.py` | Owned process handles, stdin queues, bounded output, timeouts, lifecycle receipts |
| `tools/platforms.py` | OS process launch, shell selection, capability reporting and process-tree stopping |
| `tools/git.py` | Read-only Git inspection using argument arrays through the process backend |
| `tools/recall.py` | Register v3 leased recall/progress callbacks using the same tool contract |
| `workspace.py` | Assemble the workspace services and expose the compatibility facade and artifacts |

The transport registers run-scoped callbacks with the tool registry. The session
knows no AIOS routes, worker key, claim token, operator credential, or origin UUID.
Tools return structured observations to the session. A model final reply returns
control to the runner, which executes the assignment's independent host checks.
A plan marked completed is not verification and never closes an AIOS goal.

The model receives native function schemas with `additionalProperties: false`.
Pydantic validates those same typed handlers on the host, including nested edits
and plan steps. Unknown tools, extra fields, coercions and invalid bounds are
rejected. Plain prose is never interpreted as a tool call. Successful tool results
retain their existing top-level fields and add `ok: true`. Recoverable failures
use `ok: false` and `error: {code, message}`; sensitive input values are not echoed
by schema validation. Lease/cancellation/transport failures leave the normal
execution loop rather than being mistaken for successful tool observations.

## Model-facing coding tools

| Tool | Main inputs / behavior |
|---|---|
| `inspect_path` | Existence, file type, size, permissions, owner IDs and resolved location; leaf symlink metadata without reading target content |
| `list_files` | Bounded file listing with optional continuation cursor |
| `find_files` | Workspace-relative glob, directory, result/scan limits and continuation cursor |
| `search_files` | One query or up to 16 OR queries; literal/regex, whole words, case, glob, context, matches/files/count modes and pagination |
| `read_file` | Path, line range, original byte SHA-256 and explicit line-text truncation |
| `read_files` | Up to 20 path/ranges with shared line/character budgets, hashes and remaining ranges |
| `apply_patch` | Existing exact single replacement or file creation with expected hash |
| `apply_edits` | Batch of `replace`, `create`, `delete` or `move` entries |
| `delete_file`, `move_file` | Convenience operations requiring the source hash; move destination must be absent |
| `run_command` | Exactly one of shell command text or an argument array; explicit cwd, shell, stdin and optional timeout |
| `read_process` | Process handle and byte cursor; output, exit code, timeout/cancellation and input state |
| `wait_process` | Process handle, byte cursor and wait duration (0–30 seconds); returns on output, completion, cancellation or wait timeout |
| `write_process` | Bounded UTF-8 input queued to an opted-in stdin pipe; optional EOF |
| `stop_process` | Stop an owned process tree |
| `search_process_output` | Literal/regex search of an owned command log, line context and `next_line` pagination |
| `process_output_summary` | Exit/capture status, head/tail, literal matching-line counts and bounded examples; no inference |
| `http_request` | GET/HEAD/OPTIONS to exact operator-enabled origins, bounded response excerpts and retained text artifact |
| `publish_result` | Stage findings, actual file excerpts and/or worker text for the final AIOS report |
| `process_status` | Inspect all retained handles from this execution |
| `git_status`, `git_diff`, `git_show` | Inspect status, staged/unstaged path diffs and commit changes |
| `read_artifact` | Read retained command logs or oversized observations by byte cursor |
| `host_info` | Discover OS, available shells, Python, execution scope and implemented capabilities |
| `update_plan` | Local steps with stable IDs and pending/in_progress/completed/blocked status |
| `task_blocked` | Stop execution, stop owned commands and report a missing prerequisite or clarification |
| `aios_recall`, `aios_progress` | Existing v3 leased knowledge retrieval and milestones |

### Noisy commands and filesystem diagnostics

Use `process_output_summary(process_id)` after running a noisy command. It returns
observed exit status, output completion, capture truncation, first/last lines and
literal matching-line counts for configurable terms (defaults: error, warning,
failed, traceback). These words are not a success/failure verdict: "0 errors"
still matches "error". No model is called to produce the summary.

`search_process_output` accepts `process_id`, `query`, optional `regex_mode`,
`case_sensitive`, `context_lines`, `start_line` and `limit`. Continue with the
returned `next_line` until null to cover the captured snapshot. Searches have a
roughly two-second scan budget and a 50 ms per-match regex limit. Both tools read
at most 8 MiB, return the captured byte count/hash and explicitly report incomplete
scans/capture. A live process can append or extend its last line; these are
snapshots, not subscriptions. Recheck after completion for a final view. Full
retained output remains available through `read_process`/`read_artifact`.

`inspect_path` reports missing paths as `exists: false`, and denied metadata as
`exists: null`. It reports POSIX UID/GID where available; Windows has no implied
POSIX ownership or ACL inspection. An external or dangling leaf symlink can be
identified without opening its target. Symlinked ancestors and workspace escapes
remain rejected. Metadata does not prove that a subsequent read/write will work.

### HTTP application probes

In the agent profile, open **HTTP probe destinations** and save exact origins,
one per line, for example `http://127.0.0.1:8080`. The equivalent configuration is
`"http_origins": ["http://127.0.0.1:8080"]`. Up to 20 origins are allowed. An empty
or absent list disables the HTTP tool; `host_info` shows the effective list. The
model cannot change this list through a tool argument. Scheme, hostname and port
must match; paths, credentials, query strings and wildcards are not origin entries.

```json
{"url":"http://127.0.0.1:8080/health","method":"GET","max_chars":2000}
```

This pass supports GET, HEAD and OPTIONS only. No redirects are followed, no
AIOS/provider keys or cookies are attached, and environment proxy settings are
ignored. TLS verification remains enabled. The tool is a bounded probe, not a
browser, authenticated API client or network sandbox for arbitrary shell commands.

The default response capture is 64 KiB (maximum 128 KiB), with a 6000-character
model excerpt (maximum 12000). Uncompressed text/JSON/XML responses retain a redacted text
artifact; binary or unexpectedly compressed responses return metadata/hash without
binary content. Identity encoding is requested; compressed bodies are not decompressed. The
selected response headers exclude cookies. Byte/excerpt truncation is explicit.
The time budget defaults to 10 seconds (maximum 30, capped by assignment time),
with a one-second maximum read-inactivity timeout so stalled streams yield control.
`ok: false` denotes a request error, with `status_code` if headers were observed.
An HTTP 404/500 is an observed response: `ok: true`, `http_success: false`.

### Explicit result selection

`publish_result` stages the content the conversational character needs; it makes
no immediate API call. For example:

```json
{"outcome":"partial","summary":"The application starts but its health check fails","findings":["HTTP /health returned 503"],"limitations":["Database credentials are missing"],"files":[{"path":"notes.md","start":1,"end":40}]}
```

Up to four combined `files`/`content` sections are supported. `files` selects
actual UTF-8 workspace bytes (maximum 2 MiB per file, 200 lines per range). Env
credential files and Git metadata are excluded. Selections record original byte
hashes and line ranges, preserve line endings, and redact configured credentials.
`content` accepts label/text sections (maximum 8000 characters each) explicitly
marked as worker-authored text. Each successful call replaces the prior selection;
an invalid selection leaves the prior one intact. Staged content is checkpointed
locally and survives working-context compaction; restart does not replay execution.

The runner attaches the selected report through the existing `result_summary`
finish payload after host checks, under the existing 18000-character structured
content budget and overall result limit. No extra inference or per-tool AIOS
observations are introduced. File sections identify whether their original hash
matches the final host snapshot (`true`, `false`, or null when not captured).
A matching hash is evidence of selected bytes, not independent task verification.
If execution fails after staging, retained findings return as partial/blocked with
verification unavailable. An explicit selection takes precedence over a structured
final response; existing final-response/expected-experiment-file fallback remains.
AIOS's HUD must consume `result_summary` to show the returned content.

### Search, batch reads and waiting

Searches accept one string or a list of strings in `query`; the list is an OR
search and each matching line appears once. `regex_mode` defaults to false.
`whole_word` adds Unicode word boundaries. `context_lines` includes up to five
lines on either side. For example:

```json
{"query":["/observe","tool_result","self.observe"],"pattern":"**/*.py","context_lines":2}
```

`mode: "files"` returns matching paths once; `mode: "count"` returns per-file
**matching-line counts**, not occurrences. `total_matches` describes only the
current page (matching files in files mode, matching lines otherwise). A count
page stopped mid-file marks that entry `partial: true`; add that path's counts
across pages. Empty/skipped files are not count entries.

Discovery and search return `next_cursor`. Reissue the same tool and search
options with that cursor until it is null, even if a page has no results.
Result and scan limits may change between pages. The scan budget defaults to
5000 files **per page**, rather than a permanent repository ceiling. Searches
also yield after roughly two seconds; a single regex match is limited to 50 ms
using the `regex` dependency. Invalid or excessive expressions return an error.
The final page may be empty when the previous page ended exactly at a limit.
Common dependency directories, symlinks and secret env files remain excluded;
search skips files over 1 MiB and invalid UTF-8, with per-page skipped counts.

Cursors belong to this workspace execution; the most recent 128 are retained.
They are not durable snapshots or transferable between runs. Restart searches
after adding, deleting or renaming files. A changed file being resumed mid-file
is rejected by its byte hash. Large results still use retained artifacts, while
the continuation cursor and scan/count metadata remain visible in the receipt.

```json
{"reads":[{"path":"src/caller.py","start":20,"end":90},{"path":"src/handler.py"}],"max_lines":300,"max_chars":12000}
```

`read_files` shares a default 400-line / 12000-character budget (maximum 18000
characters) across the batch. `max_chars` measures the serialized `files` array,
including hashes and line metadata, before credential redaction; the outer
receipt/remaining-range metadata is bounded separately by the normal artifact
mechanism. Each range retains the existing 200-line per-read and 2 MiB file
limits. Use the returned `remaining` array as the next `reads` input. File errors
are reported per entry; invalid scopes/ranges reject the batch. Lines clip at
1000 text characters, or less to fit the shared budget, and explicitly mark
`text_truncated`. Use a command to inspect longer line content. Nested repository
instructions load for every requested path before later edits/commands.

After `run_command`, prefer `wait_process` to repeated immediate polls:

```json
{"process_id":"<handle>","cursor":0,"wait_seconds":10}
```

Continue with the returned byte cursor. `wait_reason` distinguishes `output`,
`completed`, `workspace_stopped` and `wait_timeout`. A wait timeout does not stop
the process or mark the command timed out. Waiting is capped by the remaining
assignment wall time and responds to local cancellation. Exit status and output
completion retain their existing meaning; inspect those before claiming success.
No new AIOS endpoints or raw tool-result ingestion are introduced.

Example batch:

```json
{
  "edits": [
    {"path":"src/calc.py","expected_hash":"<sha256>","operation":"replace","old":"a-b","new":"a+b"},
    {"path":"notes.txt","expected_hash":"","operation":"create","new":"Checked the calculator.\n"}
  ]
}
```

All paths in a batch are validated before any edits. Source hashes must match;
paths cannot overlap, escapes and symlink traversal are rejected, and `.git`
metadata is excluded from file edits. Original bytes/newlines and existing modes
are preserved. Writes replace staged temporary files. If publishing fails or
cancellation is observed, the client attempts to restore already changed files.
This is **not a crash-atomic multi-file transaction or protection against external
writers**. An OS crash, failed rollback or concurrent shell write still requires
inspection. Empty directories created along the way may remain after rollback.
Limits are 100 entries, 2 MiB original files and a 4 MiB aggregate batch; individual
replacement strings and model tool-call messages have smaller bounds.

The registry bounds model-facing results. Oversized results go to redacted local
JSON artifacts with an excerpt and artifact ID. Recall status/revision metadata is
kept when truncating; full provenance remains in the artifact. Use `read_artifact`
to inspect more. Artifacts are local retained files, not remotely downloadable URLs.

## Commands and preparation for Windows/Linux work

Prefer argument arrays for commands that need no shell parsing:

```json
{"argv":["python","-m","pytest","-q"],"cwd":".","stdin":false}
```

Use a shell only when its parsing is needed. `shell: "default"` preserves the
existing platform shell behavior. `host_info` advertises available explicit
selectors: `sh`/`bash` on POSIX, `cmd`/`powershell` on Windows, and `pwsh` where
installed. An unavailable selector fails explicitly. Shell sessions are separate
processes; cwd/environment changes in one command do not persist into the next.
Host verification keeps the assignment's command text unchanged and uses the
platform default shell, as before.

Stdin is closed by default. With `stdin: true`, `write_process` queues up to 8192
UTF-8 bytes per call, with at most 16 queued items. `close_stdin: true` queues EOF
in order. A queued receipt does not assert delivery or command success. A dedicated
sender prevents a child that does not read input from blocking the model/control
thread. Output cursors refer to raw bytes; display decodes UTF-8 with replacement.
Commands keep draining output after the 8 MiB capture cap and have an effective
timeout capped by the remaining assignment budget and the ordinary 120-second
command limit. Required checks retain their explicit assignment timeouts.

Process receipts include a client handle, OS PID, command/argv, cwd, shell, log ID,
start/exit times, exit code, timeout and cancellation state. Tool intent is saved
before launch/input operations. Recovery retains these receipts and session state
for inspection; it does not attach to old PIDs or replay commands/stdin. POSIX uses
process groups. Windows currently uses the existing process-group/taskkill path;
a parent that has already exited can limit descendant cleanup. Cancellation does
not undo side effects. These are account-permission commands, **not a sandbox**.

The process backend is injectable and reports its own capabilities. The next
system-work pass can extend this seam without changing AIOS or the model-session
loop. Its concrete remaining work is:

- Add POSIX PTY and Windows ConPTY backends, with resize, terminal reads, input,
  interrupt, EOF, encoding/code-page handling and terminal session persistence.
- Add Windows Job Object ownership for reliable descendant cleanup, and test
  process-group/job behavior under crashes and parent exit.
- Define operator-configured execution profiles for system directories, working
  directories, shell choice, privilege boundaries and environment exposure.
  Keep those profiles separate from model-provided tool arguments and AIOS identity.
- Add system-specific receipts and checks for services, packages and configuration;
  keep the current immutable assignment/evidence boundary.

This pass advertises `pty: false`, workspace scope and no privilege escalation.
It does not add system administration profiles, elevation, desktop automation,
a persistent terminal, automatic worktree provisioning, or new AIOS endpoints.
Git mutations remain ordinary explicitly authorized assignment work through commands;
the dedicated Git tools are inspection tools.

## Context, plans, blocked work and recovery

The full immutable assignment stays in its own model message. The system context
contains AIOS identity and execution guidance. A separate host working-context
message contains repository guidance, local plan, active/recent process receipts,
compaction receipts and retained errors. Root `AGENTS.md` loads at session start;
ancestor/nested files load when a tool references a path/cwd. A write/command that
reveals unseen guidance is deferred until a new model turn has received it. Read
operations can discover guidance and source in the same turn. Guidance applies
within its directory scope and cannot grant additional authority.

When history exceeds the assignment's character budget, the client compacts whole
completed assistant/tool exchanges into deterministic receipts. It preserves the
assignment, loaded guidance, plan, active/recent process states, recent operation
receipts and all compacted errors. Full redacted observations remain in SQLite and
retained artifacts. Tool-call IDs remain tracked after compaction. If pinned state
cannot fit, execution stops explicitly rather than silently removing constraints
or forgetting errors. This is character-based budgeting, not a provider tokenizer.

Local checkpoints persist assignment hash, seen call IDs, plan, blocked reason,
working history, loaded guidance and compacted receipts. They support inspection
and later explicit recovery design; they do not enable automatic execution resume.
`status` and the setup page include a compact plan/blocked/compaction summary.

`task_blocked` stops owned commands and sends a v3 blocked milestone. The current
AIOS finish contract has no resumable blocked state, so the worker finishes this
execution as `interrupted`, with `task_state: blocked` and `requires_operator: true`.
It does not report successful checks or goal completion. The listener can take
another assignment; the operator must clarify/reassign with a new request ID.
Standalone v2 work retains the reason in its finish result and local journal.

The test suite exercises real temporary files, Git operations, argv/stdin/process
termination and scripted model/AIOS endpoints. CI runs the same suite on Windows
and Ubuntu with Python 3.11/3.12. It does not establish production model reliability,
full terminal support or system administration readiness.
## Inspection and provider-response diagnostics

Explicit `read-only`, `read only`, `no file creation or modification`, and
`without creating or modifying any files` restrictions select a conservative
inspection mode. This convention only reduces authority; it is not a general
natural-language policy parser. The client hides and rejects write tools,
arbitrary commands/stdin, Git subprocesses, and HTTP probes. `inspect_environment`
collects OS/kernel, workspace path, effective identity, candidate directory access,
disk space and common tool paths using fixed host APIs. It does not create test
files or invoke sudo. Tool versions and sudo privileges remain unknown unless
separately authorized and observed using `inspect_privileges` (enabled in the saved
administration profile). File reads and direct JSON/text results are
supported; client journals and audit artifacts still persist outside task output.

Update AIOS together with the client: its standard `workspace / experimental_task`
profile now uses a direct final JSON response for read-only requests instead of
requiring an on-disk experiment report. Existing queued assignments retain their
original contract; conflicting output/check requirements fail explicitly. Submit
a fresh request after updating. Custom operator checks are not silently removed.

Generation endpoint **Maximum response tokens** (`max_tokens` in configuration)
defaults to 4096 and accepts 128–32768. Set it within the provider/model's supported
limit. Each model step journals `model_response` with finish reason, available
token usage, configured output limit, and a redacted response excerpt capped at
16000 characters. Credentials and HTTP headers are not included. Local diagnostic
excerpts are not sent to AIOS as memories.

A response with `finish_reason=length` executes none of its tool calls. Empty or
malformed final JSON responses also trigger a request for a smaller complete
response. Recovery is limited to two attempts within the assignment's existing
step/time budgets; earlier completed tool calls are not replayed by the client.
Malformed tool arguments remain explicit tool errors. Failed runs send a bounded
set of partial tool observations plus the failure reason and final response
metadata, without asserting task completion or successful host verification.
