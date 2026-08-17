# Lifecycle And Failure Handling

## State And Queries

Queue and execution authority remain separate. Unfiltered monitor responses are bounded
overviews; use an exact run ID or task ID for detail. Normal queries do not load all
terminal history. Missing and internally purged run IDs are both reported as missing;
minimal replay-prevention state is not model- or Web-facing.

`unknown`, `unreachable`, and `unsupported` are observations and never overwrite an
authoritative run state.

`remote-runner dev` has no queue or execution record to query. Its foreground process,
stdout/stderr, and exit status are the complete observable lifecycle.

## Explicit Waits

Submission is detached by default. Use `wait`, `wait-cohort`, or `run --wait` only when
requested. Waiting uses bounded controller-local long polls keyed by exact run-view
etags. `wait-cohort` carries one ordered set of exact run IDs through the controller's
batched `wait-runs` endpoint instead of starting one wait process per run.
Unchanged timeouts renew CLI transport without a model turn or compute-server polling
loop.
Final process exit and stdout JSON are the completion signal.

`--until reportable` waits for checksum-verified output only when the execution outcome
is `succeeded`. Failed and stopped runs return at execution terminal state; their
checkpoint synchronization can continue independently.

For a cohort, any failed, stopped, missing, purged, attention-required, or invalidly
synchronized member returns `attention_required` immediately. A successful cohort wait
returns only after every member is reportable. For an event-driven Codex completion
path, keep one `wait-cohort` tool call attached without `--max-wait`; do not poll its
PTY or restart bounded waits from model turns.

## Stop And Cleanup

Stop one exact queued or running run through controller authority. Cleanup remains a
dry run by default and removes only verified stopped runtime/records selected by the
operator. Transport ambiguity is reported rather than converted into success.

Development sessions use a separate exact cleanup contract. The remote wrapper removes
its private session after normal success or workload failure. On handled local
interruption or lost foreground SSH, the client requests cancellation only when the
session token, process group, and process-start identity match, then performs bounded
TERM/KILL and guarded removal. Cleanup never accepts an arbitrary remote path and never
deletes the project cache, tmp root, project root, or sibling sessions.

Before creating a later session, `dev` scans only the selected project's tmp directory
and removes eligible expired orphans. Fresh, live, malformed, foreign-owned, symlinked,
or identity-ambiguous entries are retained. SIGKILL, reboot, or total network loss can
therefore leave source until a later successful invocation. Cleanup is ordinary file
removal, not secure erase; the persistent cache may retain source-derived data.

If a physical server was permanently destroyed before the normal stop handshake,
use `close-decommissioned-run` for one exact run. The command is preview-first and
requires the frozen server name plus an operator reason. Apply repeats the remote
probe and refuses closure if the endpoint is reachable, the queue is still active,
the run or physical machine has a controller lease, output synchronization is
configured for the run, or any controller record is malformed. A successful apply
records `stopped` with the decommissioning reason while preserving queue, execution,
event, runtime, and output history; it never deletes remote or controller bytes.

## Purge

`purge-run` selects one exact failed run. It requires no replacement and makes no claim
about scientific equivalence. `purge-task` preview expands to exact run IDs and freezes
those IDs in its resumable plan. It creates no permanent task-name tombstone, so later
runs may reuse the same task ID.

`--apply` deletes controller queue/execution/event records. Artifact bytes are separate:
add `--delete-artifacts --apply` to delete exclusively owned runtime, output, archive,
receipt, and worktree data. Artifact deletion remains guarded by path normalization,
overlap detection, terminal authority, and server maintenance leases.

A minimal internal run-ID tombstone may prevent replay or ID reuse. It contains no
replacement or scientific provenance and is invisible to status, Web, and run views.

## Dispatch Lease Recovery

Dispatch leases are durable: an expired lease whose owner project or run records still
exist keeps blocking acquisition for every project and blocks controller release
activation, because a crashed dispatch may have started an unknown live workload.
The owning dispatcher reconciles such leases when it next runs.

A lease whose owning queue record and execution record are both gone can no longer
protect any authorized live workload: purge only removes terminal execution records,
and terminal records mean the remote workload already ended. Such orphaned leases are
released during lease acquisition, by the owning project's reconciliation, and during
release activation. Unknown-launch outcomes keep a non-terminal `registered` execution
record, so transport ambiguity is never converted into lease release authority.

## Output Synchronization

Every terminal run with a declared output path may enqueue transfer, including
`failed` and `stopped` checkpoints. The target pulls one exact source path, verifies the
transfer with an rsync checksum dry run, commits `artifacts/<run-id>`, and writes a
receipt containing the original `authoritative_status`.

Synchronization proves byte transport, path identity, checksum, and receipt. It never
changes execution status and never declares scientific validity, eligibility, or an
official result. Pruning source output requires a matching completed receipt and keeps
the archive plus run history.

Each transfer is bounded by a hard timeout. A stuck transfer is recorded as retryable
and does not stall other pending synchronizations; controller worker cycles survive
per-cycle errors and retry after the configured interval.

## Boundary Migration

The controller release that removes experiment management performs one bounded,
idempotent migration during activation. Orphaned dispatch leases with no remaining
owner records are released first; any other dispatch lease still blocks activation.
With controller workers stopped, the migration moves the legacy experiment registry
as opaque bytes into private controller `retired-state` storage. Normal status, Web,
run views, and model-facing context never read that archive.
The move holds the legacy registry lock and leaves a minimal private retirement
marker at the former path, preventing an in-flight old binary from resurrecting the
removed subsystem. Normal controller readers never expose the marker.

Schema-1 pending output-sync intents are upgraded under the output-sync worker lock.
The migration verifies the exact succeeded run record and shared transport identity,
then drops retired scientific fields and records `authoritative_status=succeeded`.
It never infers validity from the archived data. Identity mismatches, symlinks, or
dual source/destination registries stop activation without overwriting either side;
rerunning activation resumes already completed per-project and per-intent work.

## Drain And Retirement

Drain/resume controls controller-wide admission without stopping active work. Retirement
uses one bounded preflight and an explicit preview/apply flow. Apply commits a drain,
rechecks controller revision/state, and removes project/global runner configuration,
exact local SSH blocks, known-host entries, and server-exclusive login/archive keys.
Shared credentials are preserved. Remote Runner does not stop or destroy cloud instances.
