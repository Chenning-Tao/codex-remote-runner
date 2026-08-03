# Lifecycle And Failure Handling

## State And Queries

Queue and execution authority remain separate. Unfiltered monitor responses are bounded
overviews; use an exact run ID or task ID for detail. Normal queries do not load all
terminal history. Missing and internally purged run IDs are both reported as missing;
minimal replay-prevention state is not model- or Web-facing.

`unknown`, `unreachable`, and `unsupported` are observations and never overwrite an
authoritative run state.

## Explicit Waits

Submission is detached by default. Use `wait` or `run --wait` only when requested.
Waiting uses bounded controller-local long polls keyed by the exact run-view etag.
Unchanged timeouts renew CLI transport without a model turn or compute-server polling
loop. Final process exit and stdout JSON are the completion signal.

`--until reportable` waits for checksum-verified output only when the execution outcome
is `succeeded`. Failed and stopped runs return at execution terminal state; their
checkpoint synchronization can continue independently.

## Stop And Cleanup

Stop one exact queued or running run through controller authority. Cleanup remains a
dry run by default and removes only verified stopped runtime/records selected by the
operator. Transport ambiguity is reported rather than converted into success.

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

## Output Synchronization

Every terminal run with a declared output path may enqueue transfer, including
`failed` and `stopped` checkpoints. The target pulls one exact source path, verifies the
transfer with an rsync checksum dry run, commits `artifacts/<run-id>`, and writes a
receipt containing the original `authoritative_status`.

Synchronization proves byte transport, path identity, checksum, and receipt. It never
changes execution status and never declares scientific validity, eligibility, or an
official result. Pruning source output requires a matching completed receipt and keeps
the archive plus run history.

## Boundary Migration

The controller release that removes experiment management performs one bounded,
idempotent migration during activation. With dispatch leases excluded and controller
workers stopped, it moves the legacy experiment registry as opaque bytes into private
controller `retired-state` storage. Normal status, Web, run views, and model-facing
context never read that archive.
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
