# Contributing

## Development Setup

Remote Runner requires Python 3.12 or newer, uv, Git, and tmux. On Linux, tests
that inspect process ownership also require procps.

```bash
uv sync --frozen --group dev
npm ci --prefix web
npm run build --prefix web
```

Run focused checks locally for the code you changed. For example:

```bash
uv run pytest -q tests/test_web_app.py
uv run ruff check src/remote_runner/web_app.py tests/test_web_app.py
npm run check --prefix web
```

GitHub Actions is the authoritative full validation gate. Every pull request
runs the complete Python suite, repository-wide lint and type checks, a clean web
asset build, and Linux/macOS package smoke tests. Do not duplicate that complete
matrix locally unless CI is unavailable or a failure needs local reproduction.

The committed files under `src/remote_runner/web_static` are generated from the
versioned sources in `web`. Rebuild them in the same change and confirm that a
second build leaves the worktree clean. Web UI changes follow
[`web/DESIGN.md`](web/DESIGN.md).

Tests that exercise actual SSH infrastructure must use disposable hosts and must
never add private endpoints, credentials, local workflow state, or
machine-specific paths to the repository.

## Changes

- Keep queue and execution authority separate.
- Preserve unknown transport outcomes instead of inferring success or failure.
- Add migration coverage when changing a persisted record or controller protocol.
- Preview destructive operations and require an explicit apply flag.
- Update the README or references when changing public CLI behavior.

Use focused commits and explain operational or compatibility impact in the pull
request. Public APIs and state formats follow semantic versioning from the first
stable release; during 0.x development, breaking changes must still be documented
in `CHANGELOG.md`.

## Releases

Use the version tag alone, such as `v0.3.4`, as the GitHub Release title.

## Reporting Bugs

Include the local and controller operating systems, Remote Runner version, command
shape with sensitive values removed, authoritative JSON state, and relevant error
output. Do not post SSH configuration, credentials, private repository URLs, or
workload data in a public issue.
