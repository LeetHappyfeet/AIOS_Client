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
| `tools/files.py` | Discovery, literal search, bounded reads and hash-checked edits |
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
| `list_files` | Existing bounded file listing |
| `find_files` | Workspace-relative glob, directory and result limit |
| `search_files` | Literal query, directory, glob, case sensitivity and result limit |
| `read_file` | Path, line range, original byte SHA-256 |
| `apply_patch` | Existing exact single replacement or file creation with expected hash |
| `apply_edits` | Batch of `replace`, `create`, `delete` or `move` entries |
| `delete_file`, `move_file` | Convenience operations requiring the source hash; move destination must be absent |
| `run_command` | Exactly one of shell command text or an argument array; explicit cwd, shell, stdin and optional timeout |
| `read_process` | Process handle and byte cursor; output, exit code, timeout/cancellation and input state |
| `write_process` | Bounded UTF-8 input queued to an opted-in stdin pipe; optional EOF |
| `stop_process` | Stop an owned process tree |
| `process_status` | Inspect all retained handles from this execution |
| `git_status`, `git_diff`, `git_show` | Inspect status, staged/unstaged path diffs and commit changes |
| `read_artifact` | Read retained command logs or oversized observations by byte cursor |
| `host_info` | Discover OS, available shells, Python, execution scope and implemented capabilities |
| `update_plan` | Local steps with stable IDs and pending/in_progress/completed/blocked status |
| `task_blocked` | Stop execution, stop owned commands and report a missing prerequisite or clarification |
| `aios_recall`, `aios_progress` | Existing v3 leased knowledge retrieval and milestones |

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
