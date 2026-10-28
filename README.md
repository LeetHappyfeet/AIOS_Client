# AIOS Client

AIOS Client runs an existing AIOS character in a software workspace. It calls your
OpenAI-compatible model, searches and edits files, runs commands, and returns actual
tool observations to AIOS. It is a background client with a local setup page, not an IDE.

## Install and configure

Python 3.11+, Git, and an existing development workspace are required. Linux is the
primary target; Windows process support and CI are included. Run under an
ordinary account inside a dedicated development VM or container. Shell commands have
that account's permissions: workspace file checks are not a shell sandbox.

```bash
git clone https://github.com/LeetHappyfeet/AIOS_Client.git
cd AIOS_Client
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
aios-client setup
```

On Windows activate with `.venv\Scripts\Activate.ps1` in PowerShell. Open the printed
`http://127.0.0.1:8765` URL on the same machine. The setup server binds only to loopback,
checks its Host/Origin, and requires a per-process token for changes. It is not intended
as a remotely exposed control panel. Use an SSH tunnel if necessary, preserving the
printed loopback URL.

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
server migration as well. See the [v3 integration and delegation proposal](docs/v3-integration.md)
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

## Credentials and profiles

Entered keys are stored in the OS credential store using keyring; profile JSON contains
only `keyring:...` references. There is no plaintext fallback. On headless Linux without
Secret Service or another usable keyring backend, set a process environment variable
before starting the client and enter `env:VARIABLE_NAME` instead. A model key is optional.
Remove/replace keys in setup; clear the profile reference and save after removal.

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

## Coding behavior and records

V3 adds `aios_recall(query)` for permitted character recall during execution and
`aios_progress(kind, summary)` for milestones or blocked reasons.

Tools: list/search files, read line ranges with SHA-256, exact single-occurrence edits,
start/read/stop commands, Git status and diff. Edits reject stale hashes and ambiguous
matches. Reads/searches are bounded and skip common dependency directories. Ordinary commands
have a 120-second deadline (or the remaining assignment budget) and an 8 MiB output capture cap; they keep draining output
when capture is full. Four subprocesses per run are allowed. Git diffs are also bounded.
No commits, pushes or deployments are made by the client automatically.

Default budgets are 60 model turns, 1,800 seconds, and 100,000 serialized context
characters. Structured assignments can set these limits; HTTP calls remain subject to
transport timeouts, and cancellation is cooperative rather than a hard VM deadline.
Exhaustion stops the run with its journal intact; it does not silently discard earlier
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

AIOS receives tool observations and structured `worker_evidence`. A final model reply
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
remote artifact download and automatic context compaction are later adapters/features.
