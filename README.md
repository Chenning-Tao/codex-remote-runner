# Codex Remote Runner

[简体中文](README.zh-CN.md)

Codex Remote Runner is a command-line application for submitting durable work to
a project-owned pool of remote machines. It keeps queue and execution state on a
controller host, runs an exact clean Git revision, and lets clients reconnect to
monitor, wait for, stop, or archive a run without depending on the original shell.

The project is currently pre-1.0. State formats and deployment workflows are
tested, but operators should review upgrades before applying them to active pools.

## What It Provides

- Durable queued and running workloads backed by controller-owned state.
- Automatic placement using configured capacity, availability, and priority.
- Exact Git revision preparation in detached remote worktrees.
- Foreground waits and event-driven Codex task wakeups.
- Interactive Textual and local browser dashboards with confirmed run stopping.
- Explicit stop, cleanup, purge, server drain, and output archival workflows.

```text
local CLI / Codex skill
          |
          | SSH
          v
   controller host  ------> archive target
          |
          | SSH
          v
   compute server pool
```

## Requirements

- macOS or Linux on the local and controller hosts.
- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).
- Git, OpenSSH, and tmux on the controller and compute hosts.
- rsync when output synchronization is enabled.
- Key-based, non-interactive SSH aliases for every configured connection.

Windows is not currently supported. The application executes operator-supplied
commands on remote machines and is intended for trusted project infrastructure,
not as a hostile multi-tenant scheduler.

## Install

Install the current release directly from its GitHub tag with `uv`:

```bash
uv tool install 'codex-remote-runner[tui,web] @ git+https://github.com/Chenning-Tao/codex-remote-runner.git@v0.3.1'
remote-runner --help
```

To install from a checkout instead:

```bash
git clone https://github.com/Chenning-Tao/codex-remote-runner.git
cd codex-remote-runner
uv tool install '.[tui,web]'
remote-runner --help
```

For development:

```bash
uv sync --frozen --group dev
npm ci --prefix web
npm run build --prefix web
uv run pytest -q
```

The `tui` and `web` extras are optional. Core lifecycle commands only require
PyYAML.

## Configure

Remote Runner uses two YAML files:

1. `~/.codex/remote-servers.yaml` describes shared physical capacity and SSH
   endpoints.
2. A project-owned `.remote-runner.yaml` describes the controller, source
   repository, project remotes, scheduling, and optional output archival.

Start with [examples/remote-servers.yaml](examples/remote-servers.yaml) and
[examples/project.remote-runner.yaml](examples/project.remote-runner.yaml). See
[references/configuration.md](references/configuration.md) for the complete
contract and provisioning requirements.

## Web Dashboard

Open the dashboard for one configured project:

```bash
remote-runner web --project-config /absolute/path/to/.remote-runner.yaml
```

The command binds only to `127.0.0.1`, opens the system browser, and streams the
same controller snapshot used by the TUI. Use `--no-open` to leave the browser
closed or `--port PORT` to select another local port. The browser never receives
SSH configuration. The details drawer can stop one exact queued or running
workload, change a queued workload's priority and eligible servers, and prepare
its exact revision on a newly selected compatible server before enabling it.
Queue writes use controller revisions and a bounded preparation reservation so
stale edits or work that has entered dispatch are rejected.

## Run

Remote Runner accepts only a clean committed source revision. A minimal attached
submission is:

```bash
remote-runner run \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/worktree \
  --label "smoke test" \
  --task-id "validation/smoke" \
  --result-intent supporting \
  --wait \
  --command '"$RR_PROJECT_PYTHON" -m pytest -q'
```

The command returns authoritative queue and execution state as JSON. A failed or
stopped workload is still a successfully observed wait, so inspect the reported
outcome rather than relying only on the CLI exit status.

Common follow-up commands:

```bash
remote-runner monitor --project-config /path/to/.remote-runner.yaml
remote-runner wait --project-config /path/to/.remote-runner.yaml --run-id rr-...
remote-runner tui --project-config /path/to/.remote-runner.yaml
remote-runner web --project-config /path/to/.remote-runner.yaml
remote-runner stop --project-config /path/to/.remote-runner.yaml --run-id rr-...
```

In the TUI, select a running or queued workload and press `x` to review and confirm
a stop request. The controller remains authoritative: an ambiguous transport result
is reported as unconfirmed and the dashboard refreshes instead of assuming the run
stopped.

Read [references/submission.md](references/submission.md) before changing placement,
priority, privacy, or output identity. Read
[references/lifecycle.md](references/lifecycle.md) before destructive lifecycle
operations.

## Codex Integration

[SKILL.md](SKILL.md) and [agents/openai.yaml](agents/openai.yaml) provide the Codex
skill metadata and operating contract. They complement the CLI; the Python wheel
does not install user-specific Codex configuration.

## Security And Support

Review [SECURITY.md](SECURITY.md) before deploying the controller or restricted
output-sync keys. Please use GitHub Issues for reproducible bugs and feature
requests. Security vulnerabilities should follow the private reporting process.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This project is licensed under the
Apache License 2.0.
