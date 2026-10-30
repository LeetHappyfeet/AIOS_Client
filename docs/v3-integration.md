# AIOS v3 integration and development proposal

This integration updates AIOS_Client only. It uses the existing AIOS API documented at
[AIOS revision 7a89d91](https://github.com/LeetHappyfeet/AIOS/blob/7a89d9133a96080796f49cd7117c6f03acffbb97/docs/workspace-client.md#concurrent-character-environments-first-pass-protocol-v3-capabilities).
Automatic character delegation remains a proposed AIOS server change.

## Pair and start an outbound worker

1. Upgrade AIOS through its ordinary migration procedure, including
   `20261028_02_character_environments.sql`. Prepare two already active instances
   of the same character and runtime human, with independent worlds and distinct
   source timelines. Keep SillyTavern on the conversational origin. Bind the client
   to the dedicated execution instance using its existing session/user/scope.
2. Configure only the worker's `AIOS_WORKSPACE_API_KEY` in the client's AIOS
   connection. Validate the binding and record the returned binding ID.
3. From a separate operator environment, POST to
   `/workspace-client/v3/{binding_id}/links` using `AIOS_WORKSPACE_REVIEW_KEY`:

   ```json
   {"origin_instance_id":"<conversational-instance-uuid>","share_memory":true}
   ```

   Choose `share_memory: false` when the environments should not share permitted
   knowledge. Sharing is direct, policy constrained, and does not copy transcripts.
4. Save the returned `link_id`, origin instance UUID, and actual sharing choice in
   the client profile. Set `protocol_version: 3` to require v3. Setup also offers
   `auto`, which prefers v3 and falls back to v2 on older servers; a saved link
   always requires v3. Pairing fields record operator configuration; editing them
   does not create links, grant memory access, or change the binding in AIOS.
5. Run `aios-client serve --agent "Cortana workspace"`, or choose **Listen for AIOS
   work**. The worker CLI needs no inbound listener. The optional setup page is
   loopback-only by default, or available remotely with its own login using the
   [installer](installation.md). Polling is outbound every two seconds while idle
   and works behind NAT.
6. From the operator environment submit to
   `/workspace-client/v3/{binding_id}/links/{link_id}/submit`. Include the ordinary
   immutable assignment, request UUID, and exact current origin `source_node_id`.
   For example:

   ```json
   {
     "request_id":"<new-request-uuid>",
     "source_node_id":"<current-origin-source-node-uuid>",
     "objective":"Inspect the calculator and fix addition without changing its tests",
     "required_checks":[{"id":"unit","command":"python -m unittest -v"}],
     "preserve_paths":["test_calc.py"],
     "expected_outputs":[{"path":"calc.py","kind":"file"}]
   }
   ```

   Add `goal_id` and `attempt_id` together when delegating an existing goal attempt.
   The worker never receives the operator credential. Local **Run task** / `run`
   remains for standalone v2-style submissions and refuses linked profiles.

The worker negotiates using `supported_versions` and all four advertised v3
capabilities, rather than interpreting legacy `version: 2` as a rejection of v3.
Claims send `worker_key` and `protocol_version: 3` to the ordinary claim route.
Execution HUDs use the leased v3 context route. The complete immutable assignment
is a separate model message, with its hash checked before execution.

The model gets `aios_recall(query)` and `aios_progress(kind, summary)` alongside
workspace tools. Recall identity comes from the lease, never model-selected
character IDs. Its status, provenance, and durable-knowledge flags are retained;
`unavailable` and `changed` must not be treated as empty results. Very large recall
responses are retained as redacted local artifacts with a bounded excerpt.

Host milestones cover baseline/context preparation, verification start, and
verification outcome. Models can report useful intermediate progress or a blocked
reason. Progress is a worker report, not a successful check or goal transition.
Task-start/final observations, host verification, finish, and separate human review
remain in place. Raw tool results stay in the local journal/artifacts; they are not
posted back as observations, including recall results and process polling. Model
progress is deduplicated, spaced by 30 seconds for ordinary updates, and capped at
20 reports per run (blocked reasons bypass spacing but share the cap). The three
host milestones are separate. SillyTavern gets actual status in its next HUD; immediate UI updates
require polling in that integration. This client does not modify SillyTavern.

## Cancellation, reconnect, and recovery

The worker inspects renewal responses before model requests, after model responses,
and before each model tool call. A background renewal also checks every 20 seconds
while a model/check/command is running. Cancellation sets a per-run stop event and
terminates owned process groups cooperatively, escalating where necessary. A new
command cannot start after the workspace has been stopped. The listener remains
available after remote cancellation. A pending model HTTP call can finish later;
its returned tool calls are discarded after cancellation is detected. Already
executed side effects are not rolled back.

Idle polling and persisted report delivery retry transient transport failures,
HTTP 429, and server errors. Active execution stops when renewal fails; it does
not continue with an uncertain lease. Source/authority conflicts require explicit
reconciliation. A stopped application or powered-off computer cannot be awakened
by queue polling.

SQLite retains the run ID, claim token, full claimed assignment and hash, binding,
server URL, saved pairing coordinates, pending report payloads, and remote event
cursor. The claim is committed before workspace operations. Startup marks unfinished
runs interrupted and queues an interruption finish when metadata exists; it never
reruns model or shell operations. A pre-existing pending finish keeps its exact
payload and evidence. Legacy journals remain readable; runs created before this
update lack recoverable lease metadata and need operator inspection.

Events page from the saved cursor and commit events and cursor together. They are
synced on reconnect, background renewal, and terminal delivery. They are records,
not an instruction stream and do not independently authorize execution. Pending
progress/observations keep their original UUIDs and payloads. If AIOS rejects one
with HTTP 409, it is only retired when a status read confirms cancellation or an
already terminal run, so it cannot block a cancellation finish. Source conflicts
on a running uncancelled run retain pending reports and halt for reconciliation.
An expired/obsolete finish also remains pending for operator inspection; it is not
rewritten or reauthorized automatically.

Changing a profile's AIOS server or binding while it retains claims is refused.
Reconcile and archive that profile's journal, or create a new profile for a new
binding. Inspect command artifacts and possible surviving processes after a crash
before submitting new work. No PID is reused to kill an unrelated process after
restart. Do not use automatic resubmission to resolve uncertain shell effects.

## Proposed automatic delegation inside AIOS (not implemented here)

Keep the trust boundary on the server. Add an internal delegation service invoked
from admitted-goal processing, rather than exposing an operator credential to the
model or making the client choose a character goal.

| Stage | Server responsibility |
|---|---|
| Admit | Require a live goal/attempt and executable objective; otherwise record clarification needed. |
| Resolve | Derive required execution capabilities from a validated work proposal. |
| Select | Consider only explicitly authorized, enabled environment links for the origin identity/user. |
| Check availability | Use a server-owned worker presence/capability registry with freshness expiry. The current global protocol endpoint alone does not establish worker availability or local tools. |
| Bound | Build the existing immutable assignment: objective, checks, expected outputs, preserved paths, budgets, and original goal/attempt/source coordinates. |
| Authorize | Apply existing action policy and human approval requirements. Model prose is not authorization. |
| Deduplicate | Persist a unique consideration key for origin goal/attempt, destination link, and assignment digest; use a stable request UUID on submission retries. |
| Queue | Call the existing internal submit/lifecycle service so action, run, and events commit together. |
| Wait | Record waiting for a suitable client when no authorized, fresh, capable destination exists. Reconsider on presence/policy changes without creating duplicate attempts. |
| Complete | Reuse progress, cancellation, evidence, origin HUD projection, and reviewed goal completion; late evidence cannot finish a newer attempt. |

Worker availability/capability registration is a future server contract, not an
invented extra field on today's `/bind` or `/claim`. Until that exists, configure
capabilities administratively and keep submission operator-driven. Protocol v3
advertisement is API compatibility; it is not proof that a particular machine is
online, has a compiler, or has permission to deploy.

The execution client should stay a bounded worker. That lets automatic server
submission use the same queue without another client transport or inbound server.
For rollout, test `main` with a disposable AIOS installation and scripted model,
then test a real model and inspect retained evidence before using it for important work. The unit/integration suite mocks AIOS HTTP and model responses; it does not
claim live server, production recall, or remote attestation validation.

