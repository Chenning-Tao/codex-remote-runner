# Changelog

All notable changes to this project will be documented in this file. The format is
based on Keep a Changelog, and the project intends to follow Semantic Versioning.

## Unreleased

### Changed

- Generalized the public task identity as `--task-id` and `task_id`.
- Replaced deployment-specific host examples and diagnostics with configured or
  neutral names.

### Security

- Removed local development workflow state from the tracked and packaged source.
- Added explicit source-distribution boundaries and archive content validation.

## 0.1.0

- Initial pre-release implementation of durable remote workload preparation,
  scheduling, monitoring, lifecycle control, output synchronization, and Codex
  integration.
