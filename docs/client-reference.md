# Client reference

For a short introduction and quick start, see the [README](../README.md).

AIOS Client runs an existing AIOS character in a software workspace. It calls your
OpenAI-compatible model, searches and edits files, runs commands, and returns bounded
task summaries and verification evidence to AIOS. It is a background client with a local setup page, not an IDE.

## Install and configure

For Ubuntu, use the [installer and remote UI guide](installation.md). Run as the
ordinary account that will own the client (for example `aiosagent`):

```bash
git clone --branch main https://github.com/LeetHappyfeet/AIOS_Client.git
cd AIOS_Client
bash install.sh --remote-url http://192.168.1.161:8765
~/.local/bin/aios-client setup
```

Replace the example address with your VM IP. The installer handles Python >=3.11
(including a user-local Python 3.12 when Ubuntu has 3.10), creates `.venv`, installs a
launcher and saves UI settings. Open the printed URL from another computer and log in
as **admin** with the UI password chosen during installation. Use `--local-only` for
loopback setup, or `--service` for an optional systemd user UI service. Remote access
requires authentication. Use HTTPS or a VPN on untrusted networks; see the guide for
certificates and firewall details. `main` includes the headless credentials, v3 worker, reporting/logging and
workspace inspection tools; no manual cherry-picking is needed.

For a manual installation, Python 3.11+, Git and a development workspace are required:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
aios-client setup
```

Manual setup remains loopback-only by default. On Windows activate with
`.venv\Scripts\Activate.ps1`. To enable remote access, run `aios-client configure-ui
--host 0.0.0.0 --public-url http://VM_IP:8765` and choose a password. All routes require
that login; Host/Origin checks and per-process change tokens still apply. Settings
persist for future `setup` launches. Shell commands have the account's permissions;
workspace file checks are not a shell sandbox.

1. Save an **AIOS connection**: host/IP, port, HTTP/HTTPS and optional base path.
2. Save a **generation endpoint**: complete base URL (usually ending in `/v1`), model ID,
   request timeout, and optional API key reference. Load models or enter an ID manually.
3. Save an **agent profile**: username, character ID, workspace, saved connections,
   and select an existing AIOS session with **Find existing AIOS sessions**. Manual
   instance/session/user/scope fields remain available under advanced binding settings.
4. Test the connection, test generation/tool calling, and validate the agent binding.
5. Enter a bounded coding assignment, required check commands, expected output files,
   and paths to preserve, then choose **Run task**. **Listen for AIOS work**
   polls for operator assignments sent through the workspace API. Use **Stop** to cancel.

The client supports **protocol v2 and v3**, preferring v3 when AIOS advertises its
supported version and all v3 capabilities. For linked environments apply the v3
server migration as well. See the [v3 integration and delegation proposal](v3-integration.md)
for pairing, outbound listening, recovery, and the proposed server-side automatic
delegation service. This update changes AIOS_Client only.

Legacy standalone work needs protocol v2 support and both workspace migrations.
Upgrade server and client together. Cancel/resubmit queued v1 assignments with explicit
criteria; they have no v2 assignment hash and will not execute.
Set `AIOS_WORKSPACE_API_KEY` on that server. Configure its value in the client's AIOS
connection, separately from your model API key. Missing/old protocol support is an error,
not a reason to fall back to unscoped remote execution.

Use an already-active dedicated workspace session in AIOS. The profile never bootstraps
or replaces a character. Agent username is a client worker label, not an authentication
credential. Character ID is the persistent AIOS identity key; instance UUID specifies its
experiential continuity. The human/routing user key is distinct from the agent username.
A source-head conflict stops work. Concurrent chat writes to the same instance require
later session coordination; do not use an actively changing SillyTavern session here.

## Console visibility and logs

Commands now print live in the terminal running `aios-client run`, `serve`, or
`setup` (including tasks started from the setup page). The console shows the
command/argv, working directory, shell, process ID, combined stdout/stderr, exit
code, timeout/cancellation, milestones, errors and the task result. Records go to
stderr, leaving CLI JSON results on stdout. Each record has a timestamp, agent
and run ID. Output is captured as it arrives, independently of model polling;
programs that buffer their own output may still need an unbuffered option.

The same activity is appended to `activity.log` in that agent's data directory.
The client prints its exact path when the worker starts. View recent records with:

```bash
aios-client logs --agent NAME --lines 100
```

With the default configuration, logs are under
`~/.config/aios-client/client-data/agents/<agent-hash>/activity.log`.
There are three rotating backups plus the current file, each about 5 MiB.
Per-command redacted output logs remain under `artifacts/<run-id>/<process-id>.log`;
the existing 8 MiB per-command capture limit also limits console output, with a
truncation notice. SQLite retains model/tool history and verification records.
Configured AIOS/model keys are redacted and terminal control characters are
escaped in the activity display. Other command output is recorded as produced.

Raw tool results (file reads, searches, diffs, process polls and recalled memory)
are **local only**. AIOS receives the task-start observation, bounded milestones,
the final verification summary, and the structured finish result. Model-requested
progress is deduplicated, ordinary updates are spaced at least 30 seconds apart,
and at most 20 model-requested reports are admitted per run. Blocked reasons
bypass the interval but share the cap; three host lifecycle milestones are
separate. Lease renewals and event polling continue normally. On upgrade, pending
raw tool observations are removed from the local outbox before delivery; this
does not remove observations already ingested by AIOS.

## Credentials and profiles

Save both the AIOS workspace key and model-provider key once in the setup page:

1. Choose **Private local file (headless Linux)** for each connection's key storage.
2. Paste the actual key into **Enter / replace API key**. Leave the advanced credential
   reference alone; it is filled automatically.
3. Click **Save connection** or **Save endpoint**. This stores an entered key as well as
   the profile. Then test the connection or load models.
4. On subsequent launches, run `aios-client setup` or `aios-client serve --agent NAME`
   normally. No credential exports or manual sourcing are required.

The local-file option is the default on POSIX systems. It stores **unencrypted** keys
separately from profiles in `client.json.credentials/` beside the configuration file,
using a directory with mode `700` and individual files with mode `600`. Only the owning
OS user and root should have access. The client rejects files/directories with unsafe
ownership, group/other permissions, or symlinks. This is not isolation from commands
running as that same user. Keep credentials outside the agent's project workspace.
The directory is ignored by Git; profile exports include `file:...` references, never
key values. Moving profiles to another machine requires entering its keys again.
Back up the private credential directory separately if needed; protect that backup.
With `--config PATH`, local credentials use `PATH.credentials/`.

**OS keyring** remains available through the storage selector. It requires a usable,
unlocked OS credential store and returns `keyring:...` references; a keyring failure
does not silently fall back to a file. Windows uses OS keyring or environment references
because this implementation requires POSIX permissions for local files.

Existing `env:VARIABLE_NAME` references still work for externally managed deployments.
Their variables must be present before launching the client. The reference field is
not a place to paste an actual API key. A model key is optional for endpoints that do
not require authentication. AIOS's worker key and a provider's model key are separate.
Blank password fields preserve existing keys. To replace one, enter the new key and
save the connection/endpoint. To remove one, click **Remove key**, then save the profile.
Keys are never returned to the browser. This stores client credentials only; AIOS must
still have its matching worker key configured in the server's own launch environment.

Default configuration: `~/.config/aios-client/client.json`. Override with the global
`--config PATH` option. The configuration page can import/export profiles without keys.
Multiple profiles may share connections. This pass runs one local agent at a time;
profile separation and endpoint gates provide the basis for later concurrent agents.

## Command line

```bash
aios-client --config ./client.json setup
aios-client --config ./client.json check --agent "Cortana workspace"
aios-client --config ./client.json run --agent "Cortana workspace" --task-file task.txt
aios-client --config ./client.json serve --agent "Cortana workspace"
aios-client --config ./client.json status --agent "Cortana workspace"
aios-client --config ./client.json stop --agent "Cortana workspace"
```

Ctrl+C stops work. A separate stop command writes a stop request checked every second
while a run is active. An in-flight model HTTP request may take up to its configured
timeout to return; owned commands are terminated when cancellation is observed.

## Coding runtime

The client owns a native model/tool session with a strict shared tool registry,
local plans, repository `AGENTS.md` guidance, bounded working-context compaction,
and durable checkpoints. It now supports batch file edits, file moves/deletion,
argument-array commands, explicit shell selection, queued stdin/EOF, artifact reads,
richer Git inspection, paginated literal/regex search, multiple-file reads under a
shared budget, and bounded process waits. The existing v2/v3 assignment and verification contracts
remain in place. See [coding runtime and the next system-work pass](coding-runtime.md)
for tool inputs, recovery limits, and the Windows/Linux terminal adapter roadmap.

## Coding behavior and records

V3 adds `aios_recall(query)` for permitted character recall during execution and
`aios_progress(kind, summary)` for milestones or blocked reasons.

Tools: paginated file discovery and literal/regex search, single/batch line reads with
SHA-256, exact and batched edits, start/read/wait/write/stop commands, Git status/diff/show. Edits reject stale hashes and ambiguous
matches. Reads/searches are bounded and skip common dependency directories. Ordinary commands
have a 120-second deadline (or the remaining assignment budget) and an 8 MiB output capture cap; they keep draining output
when capture is full. Four subprocesses per run are allowed. Git diffs are also bounded.
No commits, pushes or deployments are made by the client automatically.

Default budgets are 60 model turns, 1,800 seconds, and 100,000 serialized context
characters. Structured assignments can set these limits; HTTP calls remain subject to
transport timeouts, and cancellation is cooperative rather than a hard VM deadline.
Completed tool exchanges are compacted into retained receipts when context fills.
Pinned assignment/guidance and retained errors are preserved. If that state cannot fit,
exhaustion stops the run with its journal intact; it does not silently discard earlier
instructions. Small models must actually support native tool calls. Prose pretending
to call a tool is rejected by the compatibility probe, not executed.

Records are under `client-data/agents/<profile-hash>/` beside the configuration:
SQLite execution journal, pending AIOS deliveries, and full retained command artifacts.
Known configured keys are redacted from observations and log capture and omitted from
subprocess environments. Arbitrary repository secrets cannot be automatically detected;
keep this state directory private. Stop the client before backing it up. Per-command
capture is bounded; retention across runs is manual in this pass.

A process-start record is saved before execution. After a crash, an unfinished run is
marked interrupted and commands are never replayed automatically. V3 claims persist
their lease, assignment hash, report outbox, and event cursor; recovery can report
interruption without restarting execution. Inspect its journal,
processes and files before submitting another task. Pending notifications keep stable
event IDs for retries. A lost lease interrupts the assignment; it is never automatically
requeued. If assignment submission times out, its request ID is in the journal and can
be supplied with `run --request-id UUID` to resolve that same submission.

AIOS receives task summaries and structured `worker_evidence`. A final model reply
only starts host verification. Run status `finished` means execution ended, not that the
task or a character goal succeeded. `result.evidence.status` reports `passed`, `failed`,
`incomplete`, `stale`, or `not_configured`. No required checks means no verified completion.
AIOS recomputes evidence completeness against its immutable assignment before review.
Logs remain on the client, so artifact IDs are not server-local file paths or remote URLs.

## Structured assignments and completion

Use the setup fields for simple assignments, or pass JSON for per-check working
directories/timeouts and file/directory/absent expectations:

```bash
aios-client run --agent "Cortana workspace" --task-file examples/assignment.json
```

The example is a template for a calculator project, not a task for this repository.
A `.json` task file uses the assignment schema; other files remain plain objectives
with no required checks. Unknown fields, duplicate check IDs, escaping paths, and an
unpaired `goal_id`/`attempt_id` are rejected. The operator's criteria are hashed,
persisted, and supplied to the model; model tool calls cannot replace those criteria.
`preserve_paths` detects changes to named files/directories, including deletion.
It is a completion check, not filesystem isolation or an automatic rollback.

After all owned process outputs drain, the host runs required checks in order. Each
check records its command, cwd, timeout, exit code, cancellation, bounded excerpt,
retained log hash/size, timestamps, and the checked snapshot hash. All checks must pass;
expected outputs must exist with the right kind, and preserved paths must match their
baseline. If a check changes observed files, verification is stale and stops. Arrange
build-generating steps during model work; required checks should inspect the final tree.

`manifest.json` records baseline/final file hashes, types, modes, added/modified/deleted
paths (including untracked files), and a digest. Initial/final Git HEAD and bounded dirty
status are separate results. Manifests exclude `.git`, `.venv`, `node_modules`,
`__pycache__`, `.pytest_cache`, `.ruff_cache`, and this run's artifacts. Explicit expected
or preserved paths include otherwise skipped directories except `.git`. Coverage is
reported with the evidence. Unreadable files, more than 10,000 entries, files over
32 MiB, or more than 256 MiB of content prevent complete verification. Narrow the
workspace if it exceeds those budgets. These are observations of a snapshot, not
attestation or ongoing monitoring after completion; checks can be inadequate or altered
by workspace commands. Protect important test files with `preserve_paths`.

Optional `goal_id` and `attempt_id` bind evidence to an existing open AIOS goal attempt
in the same instance and source timeline. Goal-linked results wait for a human review.
The AIOS server uses a separate `AIOS_WORKSPACE_REVIEW_KEY`, which must differ from the
worker API key and must never be supplied to this client or its command environment.
See AIOS `docs/workspace-client.md` for the review API. Accepting reviewed evidence uses
the existing goal lifecycle with `verification=reviewed`; it does not manufacture a
canonical deterministic receipt or verified reward. Rejection retains evidence and
leaves goal disposition to the operator.

## Validation

```bash
python -m pip install -e '.[test]'
python -m pytest -q
```

The coding test uses an actual temporary Git working tree and real Python tests with a
scripted model endpoint; it verifies file edits, process results, AIOS event submission,
and completion reporting. It does not claim a particular local model will solve tasks.
The matching AIOS patch adds scoped assignment, lease, source-conflict and idempotency
SQL tests. No production AIOS service or real model is contacted by the test suite.

Android/desktop control, interactive PTYs, automatic provisioning, multi-agent teamwork,
and remote artifact download are later adapters/features.


### Structured work results

The existing execution loop returns a final JSON object with `outcome`
(`completed`, `partial`, or `blocked`), `summary`, `evidence`, `limitations`,
`artifacts`, and `requested_content`. Each requested content section has `label`
and `text`; file text keeps its line breaks. A file path or a completion message
is not a replacement for content the assignment asked to read.

The host sends a bounded `result_summary` alongside the existing check evidence.
This adds no model call and does not ingest individual tool results. Legacy prose
still works. For existing experimental assignments, the host can extract the
explicitly expected `aios-experiment-<request UUID>.json` report if its bytes match
the final verification snapshot. It does not collect arbitrary JSON files or
follow artifact paths from a model response. Findings remain worker claims.

Structured reporting is limited to 18,000 encoded JSON characters and four content
sections, with explicit content omission/truncation flags. The complete finish
payload keeps the existing server limit of 60,000 encoded characters; check
evidence takes priority over optional content. Source report files and the local
execution journal retain original material. Submitted bounded results are
available in AIOS operator request inspection. Older completed runs are displayed
compactly by the updated AIOS HUD; requested content is prioritized for the latest
completed result. Updating only the client cannot change an older server's HUD.


### Selecting results and probing applications

The worker can use `publish_result` to select actual file excerpts or an explicit
answer for the final structured report. It does not send the entire terminal log
or add an intermediate model loop. Selected excerpts carry original file hashes
and indicate whether they match the final host snapshot. Host checks still have
their own verification status.

For application tests, configure **HTTP probe destinations** in the agent profile.
Only exact saved origins are enabled; the list starts empty. Probes support GET,
HEAD and OPTIONS without credentials or redirects. `process_output_summary` and
`search_process_output` inspect noisy command logs locally, while `inspect_path`
reports filesystem metadata. See [coding tool contracts](coding-runtime.md) for
arguments, limits and examples.
