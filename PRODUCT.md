# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Remote Runner is primarily for individual researchers and small research teams
running scientific code on a trusted, project-owned pool of remote machines.
They need to understand what is running, where it is running, and what is still
waiting without staying attached to the shell that submitted the work.

## Product Purpose

Remote Runner submits durable research workloads to remote compute servers and
keeps authoritative queue and execution state on a controller host. Success
means a researcher can leave the original client, return later, and quickly
recover the real state and outputs of an exact submitted run.

## Positioning

Every managed run executes an exact clean Git revision through a
controller-owned lifecycle. Remote Runner combines that reproducibility with a
durable queue, automatic placement, reconnectable monitoring, and explicit
lifecycle operations across a project-owned server pool.

## Operating Context

- Researchers submit experiments, sweeps, benchmarks, reruns, and development
  tests from the CLI or the Codex skill.
- A controller coordinates placement and durable state over trusted SSH
  infrastructure.
- Users inspect the pool through the CLI or local web dashboard.
- Scientific programs may use the existing progress-reporting interface, but
  they are never required to integrate with it.
- Results may be synchronized to a configured archive target.
- Workload commands, output metadata, metrics, and scientific decisions remain
  opaque to Remote Runner.

## Capabilities and Constraints

- The current web dashboard is single-project and bound to the local machine. It
  streams the controller snapshot, can stop one exact
  queued or running workload after explicit confirmation, and can modify one or
  several selected workloads while they remain queued.
- The dashboard is an operations console for status, stop, queue priority,
  workload class, eligible servers, batch edits, capacity slots, drain/resume,
  and guarded retirement. It has no experiment or scientific-result registry.
- Progress is optional. When a program does not report progress, the interface
  shows authoritative lifecycle state without an empty or fabricated progress
  field.
- The product does not currently provide a reliable runtime, remaining-time, or
  queue-start prediction. The web interface must not imply those predictions.
- The controller, not the browser, owns queue order, capacity ranking,
  placement, and durable lifecycle state.
- Submission is detached by default. Explicit waits use controller long polling
  outside the model; status queries and synchronization never trigger model polling.
- Commands are executed unchanged. The selected core allocation is exposed as
  `RR_ASSIGNED_CORES` for the workload to interpret.
- Output synchronization proves byte transport, path identity, checksum, and
  receipt. It may preserve failed/stopped checkpoints and never rewrites execution
  status or declares scientific validity.
- Controller release activation performs the bounded state migration for this
  boundary change: removed experiment-registry bytes leave active project state,
  and legacy pending transfer intents are upgraded only after matching exact run
  and execution records. Retired bytes are not exposed through normal APIs or Web.
- The current release targets macOS and Linux and trusted project
  infrastructure, not hostile multi-tenant scheduling.
- Queued workloads support controller-owned manual ordering, `urgent`/`normal`
  priority changes, and selection among compatible project servers. Selecting a
  server that is not yet prepared starts preparation for the workload's exact
  revision and enables that server only after preparation succeeds. These
  controls never mutate work that has entered dispatch.
- Stop and queue-control web writes always delegate authority to the controller
  and use the queue state's revision to reject stale edits.
- Batch server selection applies one explicit eligible-server set to every
  selected queued workload. Each workload remains independently revision-guarded,
  and partial failures are reported without rolling back successful updates.
- Server details expose controller-wide standard and test slot limits. Queue
  details can switch queued work between those lanes. Capacity changes are
  revision-guarded, admission-only, and never preempt running work.
- Server details can pause or resume controller-wide dispatch admission for one
  server. Pausing requires explicit confirmation, never stops existing work,
  and refreshes authoritative drain state before the UI reports completion.
- Server details can permanently retire one server after a separate confirmation.
  A read-only assessment first checks controller-wide project activity, actual runner
  processes, frozen queue candidates, and output archival. Retirement preserves
  remote data and history, commits a controller-wide drain, removes managed project,
  global, local SSH, and dedicated archive credentials, and is blocked while any
  authoritative work or unverified successful output remains.
- Desktop web behavior is the acceptance target for the first queue-control
  increment. Queue-control layout and ergonomics on narrow/mobile viewports are
  explicitly outside this increment's acceptance scope.

## Brand Commitments

- Product name: Remote Runner.
- The interface is an operational research tool rather than a marketing
  surface.
- The first web version uses a light theme only.
- The web application follows familiar desktop product-UI conventions and
  established UX guidance. Usability, scanability, and predictable controls
  take priority over metaphor-driven visual themes.

## Evidence on Hand

- The controller exposes real server, active-run, queue, progress, output-sync,
  and server-drain snapshots.
- Existing product and lifecycle contracts are documented in `README.md`,
  `README.zh-CN.md`, `SKILL.md`, and `references/`.
- The repository contains no benchmark evidence supporting runtime or queue ETA
  claims, so future interfaces must not fabricate them.

## Product Principles

1. Preserve authoritative controller state over inferred convenience.
2. Keep research code integration optional and non-invasive.
3. Make exact runs and their server placement easy to recover and inspect.
4. Keep lifecycle behavior reproducible, durable, and explicit.
5. Keep scientific workflow and cross-session decisions outside the runner;
   external systems cite exact run IDs instead of copying run records.

## Accessibility & Inclusion

The web interface targets WCAG 2.2 AA. Status must never rely on color alone,
and all inspection and stop-confirmation workflows must remain keyboard accessible.
