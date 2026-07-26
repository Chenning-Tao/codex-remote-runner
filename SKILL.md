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
  preparation reuse, placement, test lanes, priority, privacy, or output identity.
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

## Wait And Report

Choose exactly one follow-up mode after submission. Keep a foreground task attached
with `run --wait` or wait for an existing run with:

```bash
remote-runner wait \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --run-id "<run-id>" \
  --until reportable
```

Keep the command session attached until it returns. Inspect `run_view.outcome` (or
`wait.run_view.outcome` from `run --wait`): a failed or stopped workload is still a
successfully observed wait and therefore may have CLI exit zero.

Use the attached path when the result must appear live in a Codex App task. The
controller wait itself runs outside the model; keep one command session attached and
do not add a model heartbeat. The App host may still resume a long-running tool turn,
so this is not the detached worker's strict zero-wait-token guarantee. Once the
authoritative result is ready, report in the same task. When the report belongs in
another App task, call `send_message_to_thread` exactly once with the trusted final
payload.

A wait deadline or `Ctrl-C` ends only the wait, never the durable run. Resume with
the same exact run ID. For several runs, report only after every requested run has a
terminal outcome or an explicit attention condition.

If the run should outlive the current Codex task and persisted history after reopening
the task is sufficient, register one detached history follow-up as the final command:

```bash
remote-runner wakeup register \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --run-id "<run-id>"
```

Repeat `--run-id` for a cohort. The worker consumes no model turns while waiting. A
succeeded run with output synchronization stays pending until synchronization is
`completed`; failed, stopped, missing, purged, or attention states wake without
waiting for output synchronization. The worker then starts one completion turn
through public `codex app-server`, supplies the exact project config and run IDs, and
records `history_committed` only after that turn finishes. The turn may use read-only
monitoring and existing logs or artifacts to diagnose failures, analyze synchronized
results, or explain attention conditions. It must not mutate or resubmit work without
an explicit user request. Waiting uses no model turn; completion analysis consumes
model tokens only after the event.

`thread_history_only` is not a live Codex App delivery guarantee. A standalone App
Server connection cannot call the App-owned `send_message_to_thread` tool or update
the desktop connection registry. Do not use detached wakeup when live in-task display
is required, and do not bridge that gap with private IPC, desktop state writes, deep
links, heartbeats, or scheduled polling. If registration fails, keep the foreground
wait instead of leaving follow-up to the user.

Pending subscriptions are durable. On macOS, `remote-runner wakeup install` is an
explicit one-time LaunchAgent change that also restores pending work after login or
reboot; do not install or uninstall it during an ordinary run. Use `wakeup list` to
inspect subscriptions and `wakeup cancel --wake-id ID` to cancel one.

Use `remote-runner monitor` for a bounded overview, then select an exact `--run-id`
or `--task-id` when details are needed. Follow the lifecycle reference when
interpreting authority, progress, output synchronization, or transport ambiguity.

## Choose Lifecycle Commands

- `remote-runner prepare`: prepare one reusable clean revision.
- `remote-runner run`: submit durable work; add `--wait` for one foreground run.
- `remote-runner wait`: block on one exact run's authoritative terminal state.
- `remote-runner wakeup`: register, inspect, or cancel a detached history follow-up.
- `remote-runner monitor`: inspect a project, task cohort, or exact run.
- `remote-runner stop`: stop one exact queued or running run through the controller.
- `remote-runner sync-pool` / `add-server`: extend eligible queued work.
- `remote-runner drain-server` / `resume-server`: control new dispatch leases.
- `remote-runner cleanup`: review or remove verified stopped runtime and records.
- `remote-runner purge-run`: remove one failed attempt under an explicit provenance policy.
- `remote-runner purge-task`: remove one explicitly discarded task and its results.
- `remote-runner sync-outputs`: configure or resume automatic succeeded-output archival.
- `remote-runner prune-outputs`: reclaim only checksum-verified synchronized sources.

## Guard Destructive Actions

- Read the lifecycle reference before stop, cleanup, purge, or prune.
- Preview cleanup, purge, and prune before adding `--apply`.
- Purge a failed run only with a named succeeded replacement or explicit
  `--no-replacement`; never infer provenance.
- Purge a task only after the user explicitly discards that exact stored task and all
  its results.
- Preserve unknown transport outcomes instead of claiming an action succeeded.
- Never preselect capacity locally, preempt running work, or rewrite lifecycle
  authority from logs, progress, or output presence.
