# Lifecycle And Failure Handling

Read this reference when interpreting monitor output, diagnosing transport,
stopping work, or cleaning records.

## Contents

- [State Interpretation](#state-interpretation)
- [Waiting For Terminal State](#waiting-for-terminal-state)
- [Event-Driven Codex Wakeup](#event-driven-codex-wakeup)
- [Structured Workload Progress](#structured-workload-progress)
- [Transport Diagnostics](#transport-diagnostics)
- [Stop](#stop)
- [Server Drain](#server-drain)
- [Cleanup](#cleanup)
- [Task Purge](#task-purge)
- [Failed Run Purge](#failed-run-purge)
- [Succeeded Output Sync](#succeeded-output-sync)
- [Synchronized Source Output Pruning](#synchronized-source-output-pruning)

## State Interpretation

Queue state and execution authority are separate. A queued record may be
`queued`, `dispatching`, `dispatched`, `failed`, or `stopped`; an execution may
be `registered`, `running`, `succeeded`, `failed`, or `stopped`.

Current records expose `workload_class` as `standard` or `test`. Historical
records without it are standard. Live slot accounting counts only verified
runner-owned processes whose remote status declares the test class.

Treat `unknown`, `unreachable`, and `unsupported` only as observations. They do
not overwrite authoritative state. A selected task with no matches returns an
empty result rather than falling back to all records.

The controller polls while durable work exists. Manual monitor is immediate and
recovers its private dispatcher when necessary.

An unfiltered monitor response is a bounded project overview. Its summary counts
all authoritative records, while its queue and execution arrays contain at most
20 compact active records each. `matched`, `returned`, and `omitted` make any
truncation explicit.

A task selector returns every queue and execution record for that task,
including terminal records and full payloads, plus a task-scoped summary. A run
selector returns the exact queue and execution record. There is no global
full-history monitor mode; drill down from the overview by task or run.
An exact run query also reports `output_sync.status`: `not_enqueued` while the
run has not produced a sync intent, `pending` or `retryable` during archival,
and `completed` only after the checksum-verified target receipt is committed.

## Waiting For Terminal State

Use the public wait command when the caller needs an automatic completion report:

```bash
remote-runner wait \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --run-id rr-0123456789abcdef \
  --until reportable
```

The first query performs an immediate monitor reconciliation and recovers the
dispatcher when necessary. Later queries use bounded controller-local long polls and
an opaque run-view etag, so waiting does not add a second high-frequency probe loop
against compute servers. Each bounded poll can reconnect independently after an SSH
failure.

The aggregate run view keeps queue and execution authority separate. A current
execution in `succeeded`, `failed`, or `stopped` is terminal. A queue-only `failed` or
`stopped` record is terminal. `dispatched` without an execution, a terminal queue
with an active execution, historical execution records, and other inconsistent
combinations are `attention_required`, not completion. Missing and purged records
also stop the wait without claiming a workload outcome. All three conditions return
immediately with a nonzero exit so the caller can report or escalate them instead of
silently polling forever.

An active execution whose launch outcome remains unknown is also
`attention_required`. Monitoring records the same condition when SSH is reachable and
a stored running execution has neither its exact process group nor tmux session. This
does not invent a terminal outcome: it exposes the verified authority/runtime
conflict so a wait or wakeup cannot remain pending forever. Unreachable or incomplete
observations remain non-authoritative and do not trigger this condition.

With the default `--until execution-terminal`, observing any authoritative terminal
outcome is a successful wait operation, so the CLI exits zero even when the workload
outcome is `failed` or `stopped`. Use `--until reportable` for user-facing completion:
a succeeded output-backed run remains attached while output sync is `pending`,
`retryable`, or `waiting_for_succeeded_state`, and completes only at `completed`.
Runs with no sync intent, plus failed and stopped runs, return at execution terminal.
Cancelled or unknown sync for a succeeded run returns `attention_required`.

A wait deadline exits nonzero and leaves the durable run untouched. The final JSON
reports `wait_status`, transport retry counts, and the aggregate `run_view`; status
messages and heartbeats go to stderr. `run --wait` performs submission and the same
wait in one command, printing the submitted run ID to stderr before waiting so an
interruption can always resume that exact run instead of resubmitting it.

Execution completion does not imply synchronized output availability. The final run
view includes current output-sync status; select `--until reportable` whenever the
consumer needs synchronized output before reporting or analysis. Progress remains a
latest observation rather than a replayable controller event stream.

## Event-Driven Codex Wakeup

Use a detached wakeup only when a durable run should outlive the current Codex task
and committing a follow-up to task history is sufficient. Register the exact run IDs
together so the cohort produces one follow-up turn:

```bash
remote-runner wakeup register \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --run-id rr-0123456789abcdef \
  --run-id rr-fedcba9876543210
```

Registration binds to `CODEX_THREAD_ID` by default, resolves and stores the absolute
Codex executable, verifies the controller's cohort-wait protocol, verifies the target
task through a read-only App Server request, persists the subscription under the
private Codex home, and starts an on-demand local worker. Run registration as the
last command before ending the Codex task. Use an explicit `--codex-thread-id` only
when the caller already owns that exact task.

The local worker waits through one controller connection per cohort batch. Waiting,
transport retries, sleep, and resume use no model turn. Any `attention_required`,
`missing`, or `purged` member makes the cohort ready immediately; otherwise every
member must be terminal. A succeeded member whose output sync is `pending`,
`retryable`, or `waiting_for_succeeded_state` is not ready until sync becomes
`completed`. A failed or stopped member does not wait for output sync. Cancelled or
unknown sync on a succeeded member wakes as an attention condition. A terminal subset
does not cause a busy loop while other members remain active.

Before contacting Codex, the worker atomically persists a minimal ready payload with
only run ID, phase, outcome, terminal source, attention reason, etag, and output-sync
status. It resumes the original task through `codex app-server --stdio`, uses the
deterministic wake ID as `turn/start.clientUserMessageId`, and keeps App Server alive
until `turn/completed`. After an ambiguous start response, it inspects task history
for that client ID before another start is permitted. This is an effectively-once
history commit: an already recorded matching turn is never intentionally duplicated.

The wake turn completes the user-facing investigation rather than merely announcing
status. Its trusted prompt includes the absolute project config and exact run IDs. It
permits read-only `remote-runner monitor` queries and inspection of existing logs or
synchronized artifacts: failed runs receive a concrete diagnosis, succeeded runs
receive result analysis, and attention conditions receive an evidence-based next
step. Remote content is treated as untrusted data. The turn must not resubmit, stop,
clean, purge, edit, or otherwise mutate state without an explicit user request.

The worker waits for the full diagnostic or analysis turn to complete before
archiving it as `history_committed`, with delivery guarantee `thread_history_only`.
Waiting consumes no model turn; the completion investigation can use model tokens and
read-only tool calls after the event. Once no subscriptions remain, the pending
marker is removed and the worker exits. There is no heartbeat or scheduled model
polling fallback.

A standalone App Server transport does not own the desktop App connection. Its
`turn/completed` event proves history persistence, not live rendering, unread state,
or an OS notification in the Codex App. Live delivery requires the App-owned
`send_message_to_thread` tool from an active App turn. Keep a foreground wait attached
when live display is required, then report in that task or call the tool once for
another task. The controller wait is outside the model, but the App host may resume a
long-running tool turn, so only the detached worker has a strict zero-wait-token
guarantee. Do not use private App IPC, desktop database/cache writes, deep links, or
recurring model polling as a bridge.

The detached worker survives normal task completion, sleep, and transient network
failure. Pending files survive process or machine failure. On macOS, explicit
one-time installation provides automatic login/reboot recovery while still running
no idle process:

```bash
remote-runner wakeup install
```

This writes and loads a user LaunchAgent whose `KeepAlive.PathState` is the pending
marker. Its environment contains the resolved executable directories needed by the
installed Python, remote-runner, Codex, and Node commands, rather than relying on
launchd's minimal default `PATH`. It starts only while work exists. Because installation changes user launch
configuration, do not perform it during an ordinary run. Inspect current state with
`remote-runner wakeup list`, cancel one pending subscription with
`remote-runner wakeup cancel --wake-id ID`, and remove the supervisor explicitly with
`remote-runner wakeup uninstall`.

If thread identity, Codex discovery, App Server preflight, or worker startup is
unavailable, registration fails explicitly. Keep `remote-runner run --wait` or
`remote-runner wait` attached; never replace a failed wakeup with a heartbeat or ask
the user to poll manually.

## Structured Workload Progress

Long-running producers emit one flushed line per update or heartbeat:

```text
[REMOTE_RUNNER_PROGRESS] {"schema_version":1,"scope":"c1_segment","stage":"decode","current":8000,"total":18000,"unit":"shots","elapsed_seconds":3600.0,"eta_seconds":4500.0,"sequence":12,"reported_at":"2026-07-21T12:00:00Z","heartbeat":false,"detail":{"errors":110,"segment_index":2}}
```

Version 1 has the following exact fields:

- `schema_version`: integer `1`.
- `scope`, `stage`, and `unit`: lowercase tokens matching
  `[a-z][a-z0-9_.-]{0,63}`.
- `current`: a nonnegative integer or `null`.
- `total`: a positive integer or `null`; when set, `current` must be set and no
  greater than `total`.
- `elapsed_seconds`: a finite nonnegative number.
- `eta_seconds`: a finite nonnegative number or `null`. Producers use `null`
  rather than extrapolating against an unrelated safety ceiling.
- `sequence`: a nonnegative integer that increases for each producer emission.
- `reported_at`: an RFC 3339 UTC timestamp ending in `Z`.
- `heartbeat`: a boolean distinguishing a repeated liveness record from a new
  counter update.
- `detail`: an optional object containing at most 32 lowercase keys and JSON
  scalar values. Strings are limited to 256 characters.

After a phase starts, producers should leave no more than 30 seconds between
flushed events. This cadence updates the workload log only. It does not change
the controller polling interval or create controller-side progress history.

The monitor validates the newest prefixed line in its log window. A malformed
newest event reports `progress.kind=invalid_progress`; absence of a valid v1
line reports `unknown_eta`. Legacy `[PROGRESS]`, `shots=...`, and free-text
`ETA=` records are intentionally not interpreted. Progress is observation only:
it cannot mark a run succeeded, failed, stopped, or stale.

## Transport Diagnostics

All runner SSH uses BatchMode, configured aliases, bounded timeouts, and private
stdin for commands or manifests.

If SSH reports `Operation not permitted` while
`CODEX_SANDBOX_NETWORK_DISABLED=1`, the local Codex execution sandbox blocked
the nested network call. Rerun the same `remote-runner` command with network
approval. An IP displayed by OpenSSH may simply be the configured alias after
resolution; it does not show that SSH config was bypassed.

Preserve genuine timeout, refusal, DNS, and authentication errors with their
original stderr. When an SSH disconnect occurs after launch, stop, or cleanup
may have started, preserve authority and report an unknown outcome.

## Stop

Stopping queued work cancels it before launch. Stopping running work proves the
runner-owned process group, sends TERM, waits, and escalates only when needed.
If ownership or termination cannot be verified, do not claim `stopped`.

Retry a stop that collides with the narrow controller dispatch transaction only
after that transaction resolves. Never preempt already running work through
queue priority.

## Server Drain

Use `drain-server --server NAME` before server maintenance or retirement. The
controller persists the drain at scheduler scope and excludes that server from new
dispatches across every project under the controller root. This applies to existing
queued jobs even when their immutable prepared-server snapshots contain the server.

A drain does not stop or migrate executions already running there. A dispatch lease
acquired immediately before the drain is already in flight; no lease can be acquired
after the drain is committed. Use normal monitoring and stopping rules for existing
executions. `resume-server --server NAME` removes the persistent drain and wakes the
invoking project's dispatcher when matching work is queued.

Controller-owned purge and synchronized-output pruning use maintenance leases. These
remain available while a server is drained, share the same server-wide exclusion as
dispatch leases, and never make the server eligible for new workload placement.

## Cleanup

Cleanup is dry-run by default. Review candidates, then pass `--apply` explicitly.
Use `--run-id` to constrain deletion to one reviewed stopped record.

For executions, cleanup requires authoritative stopped state, matching remote
stopped evidence, and absence of the exact tmux session and process group. It
removes only the remote `~/.rr/<run-id>` runtime and stopped controller record.
It never deletes outputs, source worktrees, succeeded/failed records, or legacy
evidence.

## Task Purge

Task purge is separate from stopped cleanup. Use it only after the user explicitly
states that one exact stored task and all of its results are no longer
needed. The dry run returns the correlated queue/execution inventory without
changing state; `--apply` is the destructive step.

The controller writes a permanent task tombstone before stopping work. New
submissions and dispatch transitions for that exact task are rejected. Queued and
running executions are stopped through controller authority; a dispatch collision
or unknown stop outcome keeps the purge incomplete and retryable.

After terminal and output-overlap checks, queue and execution records move into
controller-owned staging so normal monitoring no longer returns them. A durable
plan retains the coordinates needed for verified runtime, declared output,
`artifacts/<run-id>`, receipt, and output-sync deletion. Runtime deletion requires
matching remote terminal evidence plus absence of the exact tmux session and
process group; output deletion rejects roots and symlinks.

Each completed phase is idempotent. After remote cleanup, the event log is compacted
under its lock, staged records are destroyed, and the tombstone becomes permanent.
Transport ambiguity reports `attention_required`; retry the same command rather
than claiming completion.

A task containing any run referenced by an ingested experiment result is blocked
before its purge tombstone is created. Keep the task until experiment-aware
provenance tombstones can preserve the result's immutable run evidence.

Remote worktrees are revision caches, not inherently task-owned. Purge removes a
worktree only when no retained queue or execution references it and while holding a
server dispatch lease. Shared or unprovable worktrees remain and are reported.
Remote Git refs remain available for prepared manifests and require a separate
source-cache garbage collection protocol.

## Failed Run Purge

Failed run purge is narrower than task purge and does not tombstone the
containing task. It accepts one exact current run ID, is a dry run by
default, and requires an explicit provenance policy on every invocation:

```bash
remote-runner purge-run \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --run-id rr-0123456789abcdef \
  --replacement-run-id rr-fedcba9876543210
```

Use `--no-replacement` instead only when the failed attempt was intentionally
abandoned. The controller never infers a replacement from labels, timestamps,
paths, tags, or task membership. Repeat the reviewed command with `--apply` to
mutate state.

An execution target must be authoritatively `failed`; a queue-only target must
have a failed queue authority. Active, stopped, succeeded, legacy, malformed,
or inconsistent records are rejected and are never converted into an eligible
state. A named replacement must be a retained succeeded current execution in
the same exact task. Its source revision, submitted-command digest, workload
class, result intent/tags, and output metadata must match. Placement, resolved
worker command, and physical output path may differ. This proves equality only
for immutable inputs stored by remote-runner, not undeclared external inputs.

Apply creates a permanent minimal run tombstone and a resumable per-run plan,
then stages only the selected queue/execution evidence. It reuses task-purge's
server-aware output-overlap, verified remote cleanup, worktree-reference, and
event-compaction rules. Any exact, parent, or child overlap with a retained run
blocks output deletion. Failed-state output-sync cancellation records may be
removed, but evidence of a synchronized succeeded artifact blocks the purge.

The completed tombstone preserves the deleted run ID, exact task, reason,
provenance digests, and replacement/no-replacement decision. It stays outside
normal monitoring, prevents run-ID reuse, and prevents an individually named
replacement from later being purged. Successful task siblings, shared
worktrees, and all Git refs remain.

A failed run referenced by an ingested experiment result is not currently
purgeable, even when the result has not been accepted. The preview and apply paths
fail closed until experiment-aware provenance tombstones are available.

Transport ambiguity returns `attention_required`; retry the exact same command,
reason, and replacement policy. Once records have been staged, an older
controller release cannot resume the new plan. Reinstall this or a newer
release and retry. No rollback can restore output already verified and deleted.

## Succeeded Output Sync

When project `output_sync` is configured, a transition to authoritative
`succeeded` writes one durable outbox intent for every run with a resolved output
path. The state transition does not wait for network transfer. A separate controller
worker notifies the configured target, and the target pulls the named
source path into an isolated staging directory.

The target commits `artifacts/<run-id>` only after a second rsync checksum dry-run
reports no tree differences. It then writes a target receipt; only that verified
receipt lets the controller remove the pending intent. Repeated notifications are
safe: an existing target is checksummed again and acknowledged without another
copy. Interrupted staging remains resumable.

Failed and stopped runs are not enqueued. A succeeded run without an output path has
nothing to synchronize. Sync failures remain retryable outbox state and never
rewrite controller run authority. Canonical result publication remains a separate,
explicit workflow.

## Synchronized Source Output Pruning

Use `prune-outputs` to reclaim compute-server storage without discarding the
synchronized artifact or run history. It is a dry run by default, accepts an
optional exact `--run-id`, and accepts repeated `--server` filters; `--apply`
performs deletion.

When `output_sync.prune_after_sync.servers` is configured, the output-sync worker
applies the same guarded deletion automatically after archive verification and
experiment-result ingestion. It retries lease, transport, and eligibility failures
without weakening the completed synchronization receipt. Servers not in the
explicit allow-list retain their source outputs.

Only current, authoritatively succeeded runs with a completed checksum-verified
sync receipt are eligible. The receipt identity, source server, source path, and
revision must still match the retained execution manifest. Legacy outputs
without a configured `output_root` and paths that overlap retained runs are
blocked. Remote deletion requires a strict child of `output_root` and rejects
protected runtime/worktree paths or any symlink traversal.

After remote deletion, the completed receipt records the result and deletion
time while preserving `artifacts/<run-id>`, the target receipt, controller run
records, logs, and source worktrees. Repeating the command is idempotent. A
transport-ambiguous result remains unmarked and should be retried; an already
absent source path is then recorded as successfully reconciled.
