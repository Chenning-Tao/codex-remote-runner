# Submission Choices

Read this reference when a submission needs choices beyond the minimal run
flow.

## Contents

- [Result Intent And Tags](#result-intent-and-tags)
- [Experiment Plans, Bindings, And Results](#experiment-plans-bindings-and-results)
- [Source And Preparation](#source-and-preparation)
- [Resources And Placement](#resources-and-placement)
- [Output Identity](#output-identity)
- [Queue Priority And Privacy](#queue-priority-and-privacy)

## Result Intent And Tags

Every new run must declare one stable reporting intent:

- `--result-intent candidate` for work that may contribute to formal results;
- `--result-intent supporting` for validation or supporting evidence;
- `--result-intent excluded` for work that must not enter result reporting.

This is submission intent, not a scientific verdict. A candidate may later be
rejected or superseded outside the execution lifecycle, and a succeeded run may
still be supporting or excluded. Result intent never changes scheduling,
output synchronization, retention, or cleanup behavior.

Attach project-specific vocabulary with repeated `--tag KEY=VALUE` options.
Keys are open metadata rather than controller-defined categories, so use tags
such as `purpose=canary`, `campaign=historical-backfill`, or
`phase=smoke-test`. Do not encode whether a run contributes to results only in
a tag; keep that decision in `--result-intent` so monitoring can filter it.

Historical records without explicit intent are reported as `unclassified`.
Do not infer or bulk-reclassify them from labels, commands, paths, lifecycle
status, or output presence.

## Experiment Plans, Bindings, And Results

The public JSON contract names are `experiment_plan`, `run_binding`,
`experiment_result`, and `experiment_query`; compatibility is carried by the
numeric `schema_version` field rather than a version suffix in `kind`.

Preview a generic plan before publishing it:

```bash
remote-runner experiment plan preview \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --file /absolute/private/path/experiment-plan.json
```

Review the returned impact classification. Publish the same normalized content
with a new durable request identity and, normally, the preview's `impact_digest`:

```bash
remote-runner experiment plan publish \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --file /absolute/private/path/experiment-plan.json \
  --request-id "project-owned-stable-request-id" \
  --impact-digest "sha256:<preview-impact-digest>"
```

Publication is atomic and does not submit, cancel, or rerun work. Reuse a request
ID only for an exact retry. A later revision uses a new request ID and the exact
current `expected_active_design_revision_id`; a stale active-revision pointer is
a conflict, not permission to choose a newer record by time.

Create a `run_binding` template from published study, design, point, point
revision, plan, and setting identities. It may target several points, and one
result may cite several bound runs. Do not encode this relationship only in task
IDs or tags. The submission path fills absent `binding_id`, `run_id`, and
`source_revision` fields, validates the active revision, computes the digest, and
freezes the binding:

```bash
remote-runner run \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/task-worktree \
  --experiment-binding /absolute/private/path/run-binding.json \
  --output-relpath "experiments/study-key/run-output" \
  --result-intent candidate \
  --label "study point" \
  --task-id "study/point" \
  --command '"$RR_PROJECT_PYTHON" experiment.py'
```

When `expects_result_manifest` is true, the binding must name a normalized
relative `result_manifest_relpath`, the project must configure output
synchronization, the run must use a relative output directory, and its intent
must be `candidate`. The producer writes a bounded `experiment_result` with
`producer.mode: "native"` at that path. Keep aggregate metrics, confidence
intervals, evidence counts, checks, and artifact references in the manifest;
keep raw per-observation payloads in referenced artifacts.

The output-sync worker reads the exact manifest from the verified archive,
checks regular-file and size limits, verifies referenced artifact SHA256 values,
and idempotently projects it into the experiment registry. Native producers must
not call `remote-runner experiment result ingest`; direct ingestion accepts only
explicit `producer.mode: "legacy_adapter"` imports. Never derive results or
acceptance from commands, stdout, labels, output discovery, or timestamps.

An eligible result remains a candidate until an explicit acceptance request
updates the exact point-revision pointer with its expected current acceptance
identity. Queries use bounded `experiment_query` documents with server-side
filters, fields, pagination, and opaque `changed_since` cursors. See
[the experiment registry plan](../docs/plans/experiment-registry-results-dashboard.md)
for complete contract shapes and authority boundaries.

## Source And Preparation

Omit `--source-repo` to use `source.local_repo`. Use an absolute clean Git
worktree path to submit another task worktree without changing project identity,
controller, candidates, or scheduling.

Prepare once for a cohort. Reuse still verifies clean `HEAD` plus project-config
and server-registry digests. Explicit preparation must succeed without fallback;
automatic preparation may continue with a non-empty verified subset and reports
every failure.

## Resources And Placement

Use `--min-cores N` for a real capacity requirement. It filters eligibility but
does not choose a server. The controller ranks every prepared eligible candidate
using live runner-owned activity, then available cores, configured cores,
priority, and name.

Use `--server NAME` only for an explicit user requirement. `auto_select: false`
does not forbid explicit selection. Do not name a server merely because a
workload needs many cores.

Use `--server all` when a reusable task definition requires an explicit server
value and queued work should remain extensible. The queue persists
`server_scope: all`; after configuring and provisioning another enabled remote
with `auto_select: true`, run `remote-runner sync-pool`. It finds every queued
all-server job, prepares each locally available historical revision on missing
servers, atomically extends those candidate sets, and wakes the dispatcher.
Tasks do not need to be resubmitted. Fixed `--server NAME`, candidate allow-list,
and legacy jobs remain snapshots that do not follow automatic pool changes.

Append one configured server to one specific queued snapshot without resubmitting:

```bash
remote-runner add-server \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --run-id rr-0123456789abcdef \
  --server compute-d
```

The command preserves every existing candidate, prepares the queued Git revision
on the requested server, atomically appends its descriptor, and wakes the
dispatcher. It is idempotent and rejects work that has left `queued`. The target
must satisfy the queued minimum-core, test-pool, and relative-output constraints.
Historical jobs with an absolute output identity cannot gain another server.

Pool synchronization requires the queued commit objects to remain available in
the clean local source repository. It reports per-revision preparation failures
without removing existing candidates; rerunning it is idempotent. Historical
jobs with a server-specific absolute output identity remain fixed snapshots.

Use repeated `--candidate-server NAME` options when the workload has a real
server allow-list that cannot be expressed as a core requirement. The allowed
servers form one candidate pool; the controller still owns ranking and
placement. `--candidate-server` and `--server` are mutually exclusive. For a
test workload, the allow-list must be a subset of the configured test pool.

Do not combine the allow-list with a non-default `--min-cores`. The runner
rejects that combination instead of silently removing an allowed lower-core
server. Omit `--min-cores` when server identity captures the workload
requirement; its default of one keeps every allowed positive-core server,
including lower-core high-memory machines.

Prepare a reusable allow-listed pool with one manifest:

```bash
remote-runner prepare \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/task-worktree \
  --candidate-server compute-b \
  --candidate-server compute-a \
  --candidate-server compute-c \
  --out /absolute/private/path/plain-prepared.json
```

Passing that manifest to `run` preserves exactly the successfully prepared
subset of the allow-list. Do not create three single-server manifests or choose
a server in the launcher; the controller ranks the allowed candidates.

Preserve an explicit configured worker argument. Otherwise the controller adds
the selected server's full configured core count, not estimated headroom.

Use `--workload-class test` only for durable development tests that should use
the project's testing server pool even while standard work is active. Test
submissions cannot override the pool with `--server`, but may narrow it to a
hardware-specific subset with repeated `--candidate-server` options:

```bash
remote-runner run \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/task-worktree \
  --workload-class test \
  --candidate-server compute-d \
  --label "compute-d hardware test" \
  --task-id "task id or context" \
  --result-intent supporting \
  --tag purpose=hardware-test \
  --command '"$RR_PROJECT_PYTHON" -m pytest tests/test_hardware.py -q'
```

The controller ranks candidates with available test slots using live capacity.
Tests execute the exact submitted command without appending the project's
default worker argument; add any desired test parallelism explicitly in the
command.

Queue priority remains independent from workload class. Priority orders work
within the standard and test lanes, while a capacity-blocked standard head never
hides a dispatchable test head. Within a lane, a later job may backfill only onto
servers that no earlier blocked job is eligible to use, including across a
priority boundary. This preserves urgent candidate capacity without letting a
blocked urgent job leave unrelated normal-job capacity idle.

During one dispatcher pass, the controller shares one concurrent capacity probe
across the queued candidates and may launch placements on distinct servers in
parallel. Each placement still acquires the controller-global server lease and
rechecks that server before the queue entry moves to `dispatching`. A single batch
places at most one job on each server; additional test-slot work is considered by
the next immediate pass after a successful launch. The same rule applies when a
server has several standard slots.

The local web dashboard may switch an exact queued workload between `standard`
and `test`. The update is revision-guarded, must retain at least one server with
positive capacity in the target lane, and moves the workload to the tail of its
destination class and priority lane. Work that has entered dispatch or started
cannot change class.

## Output Identity

Prefer a normalized relative POSIX `--output-relpath`. Every eligible candidate
must configure its own absolute `output_root`; the controller resolves the
physical path only after placement. Do not use `$HOME`, `~`, command
substitution, absolute paths, or parent traversal in a relative identity.

The workload receives:

- `RR_PROJECT_PYTHON`: selected configured project interpreter;
- `RR_OUTPUT_PATH` and `RR_OUTPUT_DIR`: selected physical output identity;
- `RR_OUTPUT_ROOT`: selected root for relative-output jobs.

Use these variables instead of repeating server-specific paths in the command.

## Queue Priority And Privacy

Omit `--queue-priority` for normal work. Use `urgent` only when new work must
precede normal jobs that are still queued. Urgent jobs use protected-candidate
backfilling, remain ahead of normal queued work on every shared candidate, and
never preempt work already being dispatched or run. A normal job may still
backfill onto a server that no earlier blocked urgent job can use.

Add `--privacy process-title` only when the user requests best-effort Linux
Python process-list hygiene and the configured environment already provides its
dependency. It is fail-closed and not a security boundary. Omit it normally.
