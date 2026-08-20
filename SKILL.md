---
name: remote-runner
description: Run disposable dirty-tree development tests or submit, monitor, wait for, stop, purge, and synchronize durable workloads on project-configured remote servers. Use dev only for foreground direct execution; durable work executes an exact clean committed Git revision through the controller-owned lifecycle.
---

# Remote Runner

Use direct SSH only for short probes. Use `remote-runner` when work must persist,
queue, remain discoverable, or support later status, wait, stop, and artifact sync.
Use `remote-runner dev` only for an explicitly requested foreground test of current
local source that does not need those durable properties.

## Load Relevant Context

- Read [references/configuration.md](references/configuration.md) for infrastructure,
  capacity, drains, output transport, and retirement.
- Read [references/submission.md](references/submission.md) for source preparation,
  placement, priority, workload class, command, and output identity.
- Read [references/lifecycle.md](references/lifecycle.md) for status, explicit waits,
  stop, cleanup, purge, output sync, and pruning.

## Preserve The Boundary

- Treat the local Git repository as the only source authority. Require a clean committed
  `HEAD` and execute its detached remote worktree.
- Let the controller own queue order, capacity, placement, leases, and durable state.
- Treat workload commands, metrics, experiment points, and scientific decisions as
  opaque. Never infer validity, acceptance, equivalence, or an official result.
- Store and cite exact run IDs. Trellis may retain exact run IDs and cross-session
  decisions; it must not copy Remote Runner queue or execution records.
- Treat `unknown`, `unreachable`, and `unsupported` as observations, not authority.

## Run Disposable Development Tests

`remote-runner dev` is separate from the controller-owned lifecycle. It accepts dirty,
untracked, or non-Git source; rsyncs a filtered snapshot directly to one explicitly
selected compute server; streams stdout/stderr; returns the workload exit status; and
removes the private session source afterward.

```bash
remote-runner dev \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --server compute-a \
  --command 'python3 -m pytest -q'
```

When the project config defines the requested named dev profile, prefer
`--profile NAME` and do not reconstruct its opaque command or ignored source inputs in
the model turn. Use direct `--command` only when no suitable profile exists. Workloads
may consume `RR_RESOURCE_JSON`; Remote Runner supplies resource facts but never chooses
project worker topology.

It creates no run ID, queue record, Web entry, output sync, or scientific provenance.
It also acquires no controller lease. Treat both core variables as whole-machine usage
hints, avoid contention with durable work, and remember that the persistent dev cache
may retain source-derived information even after plaintext session cleanup.

## Submit Detached By Default

```bash
remote-runner run \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/worktree \
  --label "short readable label" \
  --task-id "task id or context" \
  --command '"$RR_PROJECT_PYTHON" workload.py'
```

Successful submission ends when the controller returns the run ID and queue record.
Retain that exact run ID. Do not keep the originating tool call attached unless the
user explicitly requests a foreground wait.

Use repeated `--candidate-server NAME` for an explicit eligible-server set. Use
`--min-cores N` for a generic core requirement. The controller ranks only prepared,
eligible servers using generic capacity and lane constraints.

Omitting `--cores` preserves the compatible whole-machine allocation. Use
`--cores N` only when the workload can safely share the physical machine; core
allocations are summed across standard and test lanes. Memory is telemetry only and
does not participate in admission.

Remote Runner executes the submitted command unchanged. It exposes the selected
allocation through `RR_ASSIGNED_CORES`; the workload decides whether and how to use it.
`RR_SERVER_CORES` exposes inventory separately from the consumable allocation.

## Query Or Wait

Use `monitor --run-id` for an immediate exact status query. It performs no model loop.
Status and synchronization do not trigger model polling.
Use `wait` only when explicitly requested. For an exact multi-run cohort, use one
`wait-cohort` command instead of one process per run:

```bash
remote-runner wait \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --run-id rr-0123456789abcdef \
  --until reportable

remote-runner wait-cohort \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --run-id rr-0123456789abcdef \
  --run-id rr-fedcba9876543210 \
  --until reportable
```

Keep one explicit wait attached to the same tool session. The CLI uses controller
long polls keyed by exact etags; cohort waits use one batched controller request rather
than one process per run. Unchanged timeouts renew transport inside the CLI and do not
trigger model polling. Process exit plus final stdout JSON is the completion signal.

## Keep Execution And Bytes Orthogonal

Output sync proves source/target paths, transferred bytes, checksum verification, and
receipt identity. It does not prove execution success or scientific validity. Terminal
`failed` and `stopped` runs may synchronize checkpoints. A completed receipt never
rewrites the run's execution status.

## Destructive Operations

- Preview cleanup, purge, prune, and retirement before `--apply`.
- `purge-run` identifies one exact failed run; no replacement run is required.
- `purge-task` preview expands the exact run IDs selected at that moment. It does not
  reserve or permanently tombstone the task name.
- Purge deletes controller records by default. Add `--delete-artifacts --apply` only
  when remote runtime, output, archive, and exclusively owned worktree bytes should
  also be deleted under path-overlap and lease guards.
- A minimal internal run-ID tombstone may prevent replay, but normal status, Web, and
  model-facing context must report the deleted record as missing and never expose it.

## Server Operations

Use drain/resume for admission control. Retirement is preview-first and revision-
guarded. It performs one bounded preflight, then removes runner configuration, exact
local SSH blocks, known-host entries, and server-exclusive login/archive credentials.
It preserves shared credentials and never calls cloud-provider APIs; instance shutdown
or destruction remains the user's responsibility.

## Command Map

- `remote-runner dev`: foreground filtered dirty-tree execution on one server; no
  durable lifecycle.
- `remote-runner prepare`: prepare one reusable revision.
- `remote-runner run`: submit detached work; wait only with explicit `--wait`.
- `remote-runner monitor`: query bounded status.
- `remote-runner wait`: attach to one exact run.
- `remote-runner wait-cohort`: attach once to an ordered exact-ID cohort until every
  member is reportable or one member requires attention.
- `remote-runner stop`: stop one exact run.
- `remote-runner close-decommissioned-run`: preview and close one exact unreachable
  run only after its physical server has been explicitly confirmed destroyed.
- `remote-runner cleanup`: clean verified stopped records.
- `remote-runner purge-run`: remove one exact failed record.
- `remote-runner purge-task`: preview and remove exact expanded run IDs.
- `remote-runner sync-outputs`: configure or resume artifact transport.
- `remote-runner prune-outputs`: remove checksum-verified source bytes.
