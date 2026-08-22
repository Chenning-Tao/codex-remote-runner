---
name: remote-runner
description: Submit, monitor, wait for, stop, purge, and synchronize durable workloads on project-configured remote servers; wait for one run or an exact multi-run cohort; derive one validator run from a reportable run and read back its project result JSON; or run an explicitly requested foreground test of the current dirty or untracked source. Durable work executes an exact clean committed Git revision through the controller-owned lifecycle.
---

# Remote Runner

Use direct SSH only for a short read-only probe. Anything that must persist, queue,
stay discoverable, or support later status, wait, stop, and artifact retrieval belongs
to `remote-runner`.

## Choose One Entry Point

- `dev`: an explicitly requested foreground test of current local source, including
  dirty or untracked files. No run ID, no queue record, no provenance.
- `run`: durable queued work. It requires a clean committed revision and returns the
  exact run ID that later status, waits, and artifacts hang from.
- `validate-run`: one validator derived from an already reportable run. Its revision,
  placement, run ID, and duplicate protection are derived rather than chosen.

Do not build a second scheduler, run registry, or artifact-retrieval path around these.
The workload may run its own internal workers, but server choice, allocation, queue
state, run identity, lifecycle, and artifact reads stay with Remote Runner.

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

## Derive One Validation Run

Use `validate-run` when a reportable source run must be validated by a project-owned
command. Do not hand-build the source identity, historical checkout, archive placement,
or duplicate-submission guard in the model turn.

```bash
remote-runner validate-run \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-run-id rr-0123456789abcdef \
  --validator-key portable-smoke/v1 \
  --command 'project-owned-validator-command' \
  --result-relpath acceptance.json \
  --wait --max-wait 900
```

One source run and validator key own exactly one validator run. Rerun the identical
command to resume it; never rotate to a fresh key to work around a timeout, a conflict,
or a failed validator, and change keys only when the validation itself is deliberately
different. The validator command receives `RR_SOURCE_RUN_ID`, `RR_SOURCE_REVISION`,
`RR_SOURCE_SERVER`, `RR_SOURCE_ARTIFACT_PATH`, and `RR_VALIDATOR_KEY`; keep the command
text byte-stable so an exact retry stays an exact retry.

The returned result JSON is opaque transport output. Report its fields as the project's
own claim and never restate them as Remote Runner acceptance.

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

An explicitly requested Codex background supervisor is an external delivery owner,
not a Remote Runner lifecycle feature. Give it only the canonical project config,
ordered exact source run IDs, expected revision, parent delivery target, and one frozen
validator specification per source run. It must keep one
`wait-cohort` attached without `--max-wait`, add no `monitor`, SSH, progress commentary,
or second wait, and only
reattach the same tool session when the surface requires it. Exit 0 permits one
`validate-run` per source run: run one `validate-run` per source run using its frozen
specification. Exit 4 skips validation. The external supervisor may
then deliver one terminal report, while Remote Runner remains unaware of Codex threads.

## Read The Exit Code

Waits and derived validations report their outcome in the process exit status, not only
in prose:

- `wait` and `wait-cohort`: `0` the requested condition held; `3` the observation window
  expired while the controller still owns the run; `4` a run failed, stopped, or needs
  attention.
- `validate-run`: `0` validated and the result JSON retrieved; `1` reportable but the
  guarded result read failed; `2` invalid usage, configuration, or identity contract;
  `3` observation window expired; `4` validator failure or attention.

A non-zero code is a fact to report, not a reason to resubmit. Rerun the identical
command to resume the same run; never rotate a run ID or validator key to escape one.

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
- `remote-runner validate-run`: derive one idempotent validator run from one
  reportable source run and return its opaque project result JSON.
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
- `remote-runner sync-pool`: extend queued automatic pools to newly prepared servers.
- `remote-runner add-server`: append one prepared server to one exact queued run.
- `remote-runner drain-server` and `resume-server`: control controller-wide admission
  without stopping active work.
- `remote-runner retire-server`: preview and remove one server's runner configuration.
- `remote-runner web`: open the optional local dashboard for the current project.

## Load Details Only When Needed

- [references/configuration.md](references/configuration.md): infrastructure, capacity,
  drains, output transport, retirement.
- [references/submission.md](references/submission.md): source preparation, placement,
  priority, workload class, command, output identity, derived validation.
- [references/lifecycle.md](references/lifecycle.md): status, explicit waits, exit
  codes, stop, cleanup, purge, output sync, pruning.
