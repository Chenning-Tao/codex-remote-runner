# Changelog

All notable changes to this project will be documented in this file. The format is
based on Keep a Changelog, and the project intends to follow Semantic Versioning.

## Unreleased

### Added

- Added an optional loopback-only, read-only web dashboard with live controller
  snapshots, responsive server and queue tables, filtering, and detail panels.

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
