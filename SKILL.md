---
name: remote-runner
description: Run durable experiments, benchmarks, sweeps, reruns, and development tests on project-configured remote servers. Use before preparing, submitting, waiting for, monitoring, stopping, cleaning, or inspecting work that must survive the current shell or queue across a server pool. Execute only an exact clean committed Git revision through the controller-owned lifecycle.
---

# Remote Runner

Use direct SSH only for short probes. Use the bare `remote-runner` command when
work must persist, queue, remain discoverable, or support later wait and stop.

## Load Only Relevant Context

- Read [references/configuration.md](references/configuration.md) only when
  configuring, auditing, provisioning, draining, or retiring infrastructure.
- Read [references/submission.md](references/submission.md) when choosing source,
  preparation reuse, placement, test lanes, priority, privacy, output identity,
  or experiment plans, bindings, and structured results.
- Read [references/lifecycle.md](references/lifecycle.md) when waiting, interpreting
  state, diagnosing transport, stopping, cleaning, purging, or synchronizing output.

Do not load every reference by default.

## Preserve The Contract

- Treat the local Git repository as the only source authority. Require a clean
  committed `HEAD`; execute only its clean detached remote worktree.
- Use the canonical project `.remote-runner.yaml`; never create task-, commit-, or
  server-specific copies.
- Let the controller own queue order, capacity ranking, placement, and durable state.
- Treat `unknown`, `unreachable`, and `unsupported` as observations, never authority.
- Use the bare `remote-runner` command as the lifecycle network approval boundary;
  do not reconstruct internal interpreter or install paths.
- Do not provision, install, update, or deploy infrastructure during an ordinary run.

## Submit Work

Use this foreground flow for one normal run:

```bash
remote-runner run \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/task-worktree \
  --label "short readable label" \
  --task-id "task id or context" \
  --result-intent candidate \
  --wait \
  --until reportable \
  --command '"$RR_PROJECT_PYTHON" experiment.py'
```

Omit `--source-repo` to use the configured repository. Omit
`--prepared-manifest` for one submission. Use `--workload-class test` only for a
durable development test when the project binds a configured testing pool.

Prepare once when several runs share one revision and candidate set. Submit the
whole cohort without `--wait`, retain every run ID, then wait for each exact run;
do not serialize submissions that should execute concurrently.

Use `--server NAME` only for an explicit user requirement. Use repeated
`--candidate-server NAME` for a real allow-list. Leave automatic placement to the
controller. Read the submission reference before making any non-default placement,
resource, priority, privacy, or output decision.

Treat a queued response as successful submission, not completed work. Continue to
wait or establish follow-up unless the user explicitly requests detachment.

## Register Experiments

Use the unversioned `experiment_plan`, `run_binding`, `experiment_result`, and
`experiment_query` contract names with their numeric `schema_version`. A
project-specific adapter may compile domain input into these contracts, but the
controller receives only the generic documents.

Preview every plan before publication, inspect its `unchanged`, `new`, `stale`,
and `archived` impact, then publish the exact previewed content with a durable
request ID and the returned impact digest:

```bash
remote-runner experiment plan preview \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --file /absolute/private/path/experiment-plan.json

remote-runner experiment plan publish \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --file /absolute/private/path/experiment-plan.json \
  --request-id "stable-publish-request-id" \
  --impact-digest "sha256:<preview-impact-digest>"
```

Reuse the same request ID only to retry the same publication. For an existing
study, preserve its explicit `expected_active_design_revision_id`; never select a
design or scientific result by timestamp.

Pass a `run_binding` template with exact published target identities to
`remote-runner run --experiment-binding FILE`. Leave `binding_id`, `run_id`, and
`source_revision` absent unless they are already exact; the runner allocates or
injects them and freezes the normalized binding into queue and run records. A
result-producing binding requires `--result-intent candidate`, a relative output
directory, configured output synchronization, and a normalized manifest path.

New producers write one bounded `experiment_result` with `producer.mode` set to
`"native"` at that exact path. Output synchronization verifies the manifest and
artifact digests before controller ingestion. Do not submit native results with
`experiment result ingest`; that direct command is reserved for named
`legacy_adapter` producers. Eligibility does not imply acceptance, and no result
may replace the explicit acceptance pointer without a separate acceptance action.

Use bounded `experiment_query` documents for registry reads. Request only the
needed operation, filters, fields, and page; follow opaque cursors instead of
opening controller SQLite or scanning command text, stdout, labels, or timestamps.

## Wait And Report

This is the only automatic Codex follow-up path. When the current Codex App task must
report automatically, do not end the originating turn after submission. Keep
`run --wait` attached, or wait for an existing run with:

```bash
remote-runner wait \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --run-id "<run-id>" \
  --until reportable
```

Keep that exact command session attached until the process exits. If the shell tool
yields a live session handle, continue waiting on the same handle; do not replace the
wait with `monitor` calls. The CLI uses etag-based controller long waits outside the
model. A state change returns early; an unchanged bounded timeout only renews the
transport inside the CLI. It is not completion and must not trigger a model turn or a
separate `monitor` or status call.

Treat only process exit plus the final authoritative stdout JSON as the completion
signal. State transitions and unchanged long-wait timeouts may appear on stderr and
are status only. Tool completion resumes the originating App turn, where Codex can
inspect existing logs or synchronized artifacts and produce the final response. The
App owns focus, unread, and notification behavior: never promise a blue dot or OS
notification, and never write App state to manufacture one.

With no `--max-wait`, waiting has no total-duration limit. Controller transport
failures also retry indefinitely unless the user explicitly supplies
`--connection-grace`. A wait deadline, an explicit connection-grace failure, or
`Ctrl-C` ends only the wait and never the durable run; resume using the same exact run
ID instead of resubmitting.

Inspect `run_view.outcome` (or `wait.run_view.outcome` from `run --wait`): a failed or
stopped workload is still a successfully observed wait and may have CLI exit zero. A
succeeded output-backed run is reportable only after output synchronization completes.
Once the result is authoritative, report in the same turn. When the report belongs in
another active App task, call `send_message_to_thread` exactly once with the trusted
final payload.

For several runs, submit them concurrently, retain every exact run ID, and wait for
each before reporting the cohort. If the originating App turn or tool session ends,
state that no automatic App follow-up will occur; the remote run remains durable and
can be reattached later by exact run ID. Do not add a model heartbeat, shell callback,
standalone App Server, private App IPC, or scheduled model/tool poll as a substitute.

Use `remote-runner monitor` for a bounded overview, then select an exact `--run-id`
or `--task-id` when details are needed. Follow the lifecycle reference when
interpreting authority, progress, output synchronization, or transport ambiguity.

## Choose Lifecycle Commands

- `remote-runner prepare`: prepare one reusable clean revision.
- `remote-runner run`: submit durable work; add `--wait` for one foreground run.
- `remote-runner experiment`: preview and publish plans, run bounded queries,
  record explicit acceptance, or rebuild the experiment projection.
- `remote-runner wait`: block on one exact run's selected authoritative condition.
- `remote-runner monitor`: inspect a project, task cohort, or exact run.
- `remote-runner stop`: stop one exact queued or running run through the controller.
- `remote-runner sync-pool` / `add-server`: extend eligible queued work.
- `remote-runner drain-server` / `resume-server`: control new dispatch leases.
- `remote-runner retire-server`: assess and permanently remove one server's scheduling and dedicated connection configuration.
- `remote-runner cleanup`: review or remove verified stopped runtime and records.
- `remote-runner purge-run`: remove one failed attempt under an explicit provenance policy.
- `remote-runner purge-task`: remove one explicitly discarded task and its results.
- `remote-runner sync-outputs`: configure or resume automatic succeeded-output archival.
- `remote-runner prune-outputs`: reclaim only checksum-verified synchronized sources.

## Guard Destructive Actions

- Read the lifecycle reference before stop, cleanup, purge, or prune.
- Preview manual cleanup, purge, and prune before adding `--apply`; configured
  post-sync pruning is an explicit persistent policy and uses the same receipt,
  path, overlap, and maintenance-lease guards.
- Purge a failed run only with a named succeeded replacement or explicit
  `--no-replacement`; never infer provenance.
- Purge a task only after the user explicitly discards that exact stored task and all
  its results.
- Preserve unknown transport outcomes instead of claiming an action succeeded.
- Never preselect capacity locally, preempt running work, or rewrite lifecycle
  authority from logs, progress, or output presence.
