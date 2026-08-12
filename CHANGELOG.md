# Changelog

All notable changes to this project will be documented in this file. The format is
based on Keep a Changelog, and the project intends to follow Semantic Versioning.

## 0.9.4 - 2026-08-12

### Added

- Added `remote-runner dev`, a foreground-only path that transfers a filtered dirty,
  untracked, or non-Git source snapshot directly to one trusted compute server without
  weakening the clean-revision controller lifecycle. It streams workload output and
  status, provides whole-machine build parallelism defaults, keeps an explicit project
  cache, and guard-cleans the private session source on completion or recovery.

## 0.9.3 - 2026-08-12

### Added

- Added a preview-first `close-decommissioned-run` lifecycle for exact nonterminal
  runs whose physical server was explicitly confirmed destroyed. Closure requires a
  fresh unreachable probe, no active queue or machine lease, no output-sync contract,
  and an operator reason; it records a stopped terminal state without deleting
  controller history, remote runtime, or output bytes.

## 0.9.2 - 2026-08-12

### Added

- Added stable controller-wide `machine_id` values backed by remote machine
  fingerprints, with fail-closed alias/conflict checks and migration of legacy
  name-keyed capacity, drain, and lease state.
- Added explicit `--cores N` allocations and `RR_SERVER_CORES`. Core allocations
  are conserved across standard and test lanes, while omitted requests and old
  queued manifests retain whole-machine allocation semantics.

### Changed

- Made controller activation transactionally align the client CLI, controller-global
  CLI, and private `runner/current` runtime, with exact revision receipts and rollback
  on mismatch. Memory remains telemetry and inventory rather than admission authority.

### Fixed

- Covered the complete remote startup window with renewable fenced dispatch leases;
  unknown launches and interrupted dispatchers retain durable ownership until remote
  state is safely reconciled.
- Made malformed or unreadable leases block acquisition, dispatch, and release
  activation instead of being overwritten or silently ignored.
- Accepted and validated allocation-bearing remote status schema 2 while preserving
  schema-1 runtime compatibility.

## 0.9.1 - 2026-08-10

### Fixed

- Restored resumable `purge-run --delete-artifacts` for failed runs with execution
  records by keeping schema-2 run tombstones minimal and validating task identity
  against the locked execution manifest.

## 0.9.0 - 2026-08-03

### Removed

- Removed the unused Textual TUI, its `tui` CLI command, and its optional
  dependency; the local Web dashboard remains the human status interface.
- Removed the experiment plan/design/point registry, run bindings, structured
  result ingestion and decisions, official-result pointers, experiment CLI and
  controller APIs, Web Experiments views/demo, scripts, documentation, and tests.
- Removed result-intent metadata and worker-policy command rewriting. Workload
  commands now execute unchanged and receive `RR_ASSIGNED_CORES`.

### Changed

- Made controller release activation migrate the removed experiment registry out of
  active project state into private `retired-state` storage and upgrade verified
  schema-1 pending output-sync intents to the transport-only schema. Migration is
  bounded, idempotent, and blocks on identity conflicts instead of overwriting data.
- Made durable submission detached by default; explicit waits continue to use
  controller etag long polling without model polling.
- Made output synchronization a terminal-run byte-transfer contract. Failed and
  stopped checkpoints can be archived, receipts preserve the original execution
  status, and synchronization never changes execution authority.
- Simplified purge to exact run IDs and explicit task previews. Replacement runs
  and scientific-equivalence checks are gone; task names are not tombstoned.
  Record deletion is the default, while `--delete-artifacts` explicitly selects
  separately guarded runtime/output/archive/worktree deletion.
- Kept minimal internal run-ID replay tombstones outside normal status, Web, and
  model-facing run views.
- Clarified that Trellis stores exact run-ID references and cross-session decisions,
  not copies of Remote Runner records.

## 0.8.1 - 2026-07-31

### Fixed

- Allowed dashboard `add-server` batches and `sync-pool` to prepare exact queued
  historical revisions from a verified clean linked worktree when the configured
  checkout has unrelated local changes, while preserving explicit-source priority,
  per-revision object checks, structured source audit data, and fail-closed behavior.

## 0.8.0 - 2026-07-31

### Added

- Added guarded server retirement in the CLI and Web dashboard, with a controller-
  wide multi-project assessment, actual process and output-archive checks, repeated
  post-drain validation, and explicit cleanup of project/global entries, exact SSH
  aliases, known-host records, and exclusive archive-source credentials.
- Added live physical-memory telemetry to server probes and the Web dashboard,
  including total, available, used, and percentage fields with Linux and macOS
  fallbacks when the remote host exposes them.

## 0.7.0 - 2026-07-29

### Changed

- Made the attached `wait --until reportable` tool session the sole Codex follow-up
  path: command completion resumes the same App turn for native reporting, while
  controller waiting remains outside the model.
- Retried controller transport failures indefinitely by default during an attached
  wait; `--connection-grace` now opts into a bounded outage window.

### Fixed

- Returned successful Web queue-update responses before refreshing the dashboard
  snapshot, so a slow controller refresh no longer holds the mutation response open.

### Removed

- Removed the detached `wakeup` command, standalone App Server delivery worker, and
  macOS LaunchAgent supervisor because history-only delivery cannot update the Codex
  App's unread or completion state.

## 0.6.4 - 2026-07-29

### Added

- Added a queue control that moves queued work directly to the front of its
  scheduling lane while preserving controller revision conflict protection.

## 0.6.3 - 2026-07-29

### Fixed

- Made staged controller virtual environments relocatable so installed console
  scripts remain executable after the release directory is atomically activated.

## 0.6.2 - 2026-07-29

### Added

- Added an explicit worker policy that is frozen at submission, defaults to
  automatic core-count injection for standard work and exact execution for test
  work, and remains unchanged when a queued run switches workload class.

### Fixed

- Made experiment registry catch-up incremental and transactional, skipped
  already-projected synchronized result manifests, kept post-sync source pruning
  out of immutable verification digests, and limited status reads to bound runs.
- Routed sub-agent wakeup registrations to their root Codex task, deferred failed
  deliveries without blocking other subscriptions, and allowed cancellation of a
  stale delivering subscription after its worker exits.

## 0.6.1 - 2026-07-28

### Added

- Exposed each finalized run binding to its bound workload as a canonical,
  read-only launch asset through `RR_EXPERIMENT_BINDING_PATH` and its exact
  asset digest through `RR_EXPERIMENT_BINDING_SHA256`, enabling native result
  producers to cite the exact controller-submitted binding identity.

## 0.6.0 - 2026-07-28

### Added

- Added optional static server memory metadata to the global registry and web
  dashboard, including dedicated hardware columns, detail-panel visibility, and
  persistent name, core-count, or memory sorting with explicit direction.

## 0.5.0 - 2026-07-28

### Added

- Added opt-in, checksum-verified pruning of synchronized source outputs for an
  explicit allow-list of configured servers.
- Added confirmed dashboard controls to drain or resume individual servers.
- Added cross-page batch updates for queued workload class and priority while
  preserving any scheduling settings the user leaves unchanged.
- Added explicit Web accept/reject decisions for eligible experiment candidates,
  including candidate metrics, observations, source runs, and required reasons.

### Fixed

- Kept later items in a scheduling batch on the revision produced by earlier
  lane-changing items without suppressing real concurrent-edit conflicts.
- Reduced live experiment loading to one consistency-locked dashboard query for
  the current 280-point design and kept existing table data visible during refreshes.

## 0.4.0 - 2026-07-27

### Added

- Added controller-owned experiment registry contracts, CLI operations, frozen
  run bindings, structured result ingestion, and read-only web views.
- Added result tables, curves, matrices, detail audit views, and a bundled
  `decoder_atomloss` project snapshot for controller-free dashboard evaluation.

### Changed

- Required explicit acceptance before an eligible experiment result becomes
  current; timestamps and CSV presence never imply acceptance.

### Fixed

- Kept live queue snapshots from re-enabling an in-flight batch server update and
  coalesced duplicate batch requests so they cannot race on preparation revisions.
- Reported batch preparation as completed work against a stable submitted total,
  instead of presenting a shrinking remaining count as a new preparation estimate.

## 0.3.6 - 2026-07-27

### Changed

- Reused the managed SSH control connection while preparing source revisions and
  launching workloads, avoiding redundant SSH handshakes.

### Fixed

- Kept sequential batch queue updates on the latest controller revision so one
  update no longer invalidates the remaining updates in the same batch.

## 0.3.5 - 2026-07-26

### Added

- Added cross-page queue selection and batch eligible-server updates to the web
  dashboard, including shared compatibility filtering and per-task preparation
  counts.
- Added a revision-guarded batch queue API that reports successful and failed
  workload updates independently without rolling back completed changes.

### Changed

- Kept failed workloads selected after a partial batch update and exposed every
  controller error with its task and run identifiers.
- Improved the batch editor's pending, success, keyboard, focus, and responsive
  states.

## 0.3.4 - 2026-07-26

### Fixed

- Restored the web dashboard by keeping the exact `react` and `react-dom`
  versions aligned, and made the web build reject future version mismatches.

## 0.3.3 - 2026-07-26

### Changed

- Labeled server utilization consistently as one-minute load in the TUI and web
  dashboard.
- Updated the supported Starlette and React dependency lines.

### Fixed

- Gave detached wakeup turns explicit read-only network access and disabled
  interactive approvals so unattended `remote-runner monitor` diagnostics can
  reach the controller over SSH.

## 0.3.2 - 2026-07-26

### Added

- Added controller-wide, revision-guarded standard and test slot limits that can
  be edited directly from each server's web details panel.
- Added web queue controls for switching an exact queued workload between the
  standard and test scheduling lanes.
- Added `--until reportable` so foreground waits keep successful output-backed
  runs attached until checksum-verified output synchronization completes.

### Changed

- Delayed successful detached follow-ups while output sync is pending or retrying,
  while preserving immediate failure, stop, and attention reports.
- Made detached wake turns finish read-only failure diagnosis or synchronized-result
  analysis instead of merely announcing terminal status.

### Fixed

- Report standalone App Server completion as `history_committed` with a
  `thread_history_only` guarantee instead of claiming live Codex App delivery.
- Preserve the executable search path in the wakeup LaunchAgent so Homebrew-based
  Codex installations can resolve their Node interpreter.
- Wake with `attention_required` when a launch remains ambiguous or a reachable
  server has no runtime for an execution still recorded as active.

## 0.3.1 - 2026-07-25

### Changed

- Reduced controller overview latency by loading queue records once per request and
  using PyYAML's safe C loader when available for registry and configuration reads.

## 0.3.0 - 2026-07-25

### Added

- Added controller-owned up/down ordering for queued workloads within their
  workload-class and priority scheduling lane.
- Added revision-guarded web controls for switching queued workloads between
  `urgent` and `normal` priority and selecting their eligible servers.
- Added preparation of an exact submitted revision when a compatible selected
  server is not ready, with a bounded reservation that prevents dispatch while
  preparation is in progress.

### Changed

- Exposed server compatibility metadata to the loopback dashboard without
  exposing SSH configuration or remote paths.
- Preserved explicitly selected eligible-server sets when the prepared server
  pool later changes.

### Fixed

- Made queue ordering operate on authoritative scheduling lanes and reject stale
  or no-longer-queued edits instead of accepting ineffective moves.
- Kept the previous priority and eligible-server selection when preparation or
  queue mutation fails.

## 0.2.0 - 2026-07-25

### Added

- Added an optional loopback-only web dashboard with live controller snapshots,
  server and queue tables, filtering, pagination, and task details.
- Added confirmation-gated web and TUI actions for stopping selected queued or
  running workloads through the controller-owned lifecycle.

### Changed

- Redesigned the web dashboard as a light, Simplified Chinese desktop interface
  with local timestamps and a viewport-fixed details drawer.
- Returned the complete active queue to the web dashboard and paginated it in
  fixed groups of 20 while preserving the bounded CLI overview.

### Fixed

- Kept live but unregistered server processes visible while preventing controller
  stop actions that cannot be authorized for them.
- Refreshed authoritative state after stop attempts and replaced raw controller
  command-line failures with concise user-facing errors.

## 0.1.1 - 2026-07-25

### Changed

- Parallelized candidate-pool, controller-capacity, and active-run probes while
  preserving candidate order and per-server endpoint fallback.
- Reused short-lived SSH control connections across lifecycle probes and overlapped
  dashboard status collection with its server-capacity snapshot.

## 0.1.0 - 2026-07-24

### Added

- Initial pre-release implementation of durable remote workload preparation,
  scheduling, monitoring, lifecycle control, output synchronization, and Codex
  integration.

### Changed

- Generalized the public task identity as `--task-id` and `task_id`.
- Replaced deployment-specific host examples and diagnostics with configured or
  neutral names.

### Fixed

- Stabilized the TUI probe countdown and snapshot age at integer boundaries.

### Security

- Removed local development workflow state from the tracked and packaged source.
- Added explicit source-distribution boundaries and archive content validation.
- Updated the pytest development dependency to a release that fixes
  CVE-2025-71176.
