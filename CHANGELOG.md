# Changelog

All notable changes to this project will be documented in this file. The format is
based on Keep a Changelog, and the project intends to follow Semantic Versioning.

## Unreleased

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
