# Changelog

All notable changes to this project will be documented in this file. The format is
based on Keep a Changelog, and the project intends to follow Semantic Versioning.

## Unreleased

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
