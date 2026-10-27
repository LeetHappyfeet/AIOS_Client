# AIOS Client

AIOS Client runs an existing AIOS character in a software workspace. It calls your
OpenAI-compatible model, searches and edits files, runs commands, and returns actual
tool observations to AIOS. It is a background client with a local setup page, not an IDE.

## Install and configure

Python 3.11+, Git, and an existing development workspace are required. Linux is the
primary first-pass target; Windows process support and CI are included. Run under an
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
5. Enter a bounded coding assignment and choose **Run task**. **Listen for AIOS work**
   polls for operator assignments sent through the workspace API. Use **Stop** to cancel.

The AIOS server needs the matching workspace-client companion patch and migration.
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

Tools: list/search files, read line ranges with SHA-256, exact single-occurrence edits,
start/read/stop commands, Git status and diff. Edits reject stale hashes and ambiguous
matches. Reads/searches are bounded and skip common dependency directories. Commands
have a 120-second deadline and an 8 MiB output capture cap; they keep draining output
when capture is full. Four subprocesses per run are allowed. Git diffs are also bounded.
No commits, pushes or deployments are made by the client automatically.

The working loop is bounded to 60 model turns and 100,000 serialized context characters.
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
marked interrupted and commands are never replayed automatically. Inspect its journal,
processes and files before submitting another task. Pending notifications keep stable
event IDs for retries. A lost lease interrupts the assignment; it is never automatically
requeued. If assignment submission times out, its request ID is in the journal and can
be supplied with `run --request-id UUID` to resolve that same submission.

AIOS receives tool observations as tool-origin evidence. Final reports are explicitly
marked `worker_report`; a completed coding run does not independently prove a goal.
Logs remain on the client, so artifact IDs are not server-local file paths or remote URLs.

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
