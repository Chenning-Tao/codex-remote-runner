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
- Attached Codex waits that resume the originating App turn when the wait tool completes.
- Interactive Textual and local browser dashboards with confirmed run stopping.
- Controller-owned experiment registry with frozen bindings and verified results.
- Explicit stop, cleanup, purge, server drain/retirement, and output archival workflows.

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
uv tool install 'codex-remote-runner[tui,web] @ git+https://github.com/Chenning-Tao/codex-remote-runner.git@v0.8.1'
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
remote-runner web \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/worktree
```

The command binds only to `127.0.0.1`, opens the system browser, and streams the
same controller snapshot used by the TUI. Server rows include live load and
physical-memory usage when the remote host exposes it. Use `--no-open` to leave
the browser closed or `--port PORT` to select another local port. The browser
never receives SSH configuration. The details drawer can stop one exact queued or running
workload, change a queued workload's priority and eligible servers, and prepare
its exact revision on a newly selected compatible server before enabling it. It
can also switch queued work between the standard and test lanes and edit each
server's controller-wide standard/test concurrency limits. Slot limits control
admission only and do not rewrite worker counts or stop running work. Workload
class changes preserve the worker policy frozen when the run was submitted.
Queue rows can be selected across pages to batch-change workload class, priority,
and optionally one compatible server set. Unselected settings remain unchanged.
Queue writes use per-run controller revisions and a bounded preparation
reservation so stale edits or work that has entered dispatch are rejected; batch
operations report partial failures explicitly.
Server details can also assess and permanently retire a machine after a second
confirmation. The assessment covers every project under the controller root, actual
runner processes, frozen queue candidates, and output archival. Retirement drains
controller-wide admission and removes project, global, local SSH, and dedicated
archive-source credentials while preserving shared login keys, history, runtime
directories, and outputs.

`--source-repo` is optional but recommended when queue controls may prepare new
servers. It names the clean local worktree used to push each exact queued historical
revision. If it is omitted and configured `source.local_repo` is dirty, historical
queue preparation may use a clean linked worktree registered to the same Git common
directory. The backend verifies every requested commit object and reports the selected
path and selection mode in its structured preparation result. It never stashes,
commits, resets, or submits uncommitted files. With no trusted clean source it fails
the preparation and preserves the prior queue settings.

## Experiment Registry

The Experiments section indexes controller-owned published generic designs, exact
point revisions, frozen run bindings, verified structured result manifests, and
explicit result decisions. The browser uses bounded controller queries and lets
an operator accept or reject an eligible candidate after inspecting its metrics,
observations, and source runs. It never selects a result by timestamp or falls
back to synthetic data.

Open `?demo=experiments` to inspect the bundled `decoder_atomloss` project
snapshot without a Controller. This static preview is test data for the dashboard
and never writes to Controller state; the normal Experiments view remains backed
only by the configured Controller registry.

```bash
remote-runner experiment plan preview \
  --project-config /path/to/.remote-runner.yaml \
  --file experiment-plan.json

remote-runner experiment query \
  --project-config /path/to/.remote-runner.yaml \
  --file experiment-query.json
```

Use `remote-runner run --experiment-binding binding.json` to finalize and freeze
a binding for the exact run ID and Git revision. Native result producers emit an
`experiment_result` manifest into synchronized output. A bound workload receives
the canonical finalized binding at the read-only path named by
`RR_EXPERIMENT_BINDING_PATH`, with its file digest in
`RR_EXPERIMENT_BINDING_SHA256`; unbound workloads do not receive either variable.
Eligible results still require an explicit acceptance action. See
[the implementation plan](docs/plans/experiment-registry-results-dashboard.md)
for contracts, authority boundaries, and remaining hardening work.

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
  --until reportable \
  --command '"$RR_PROJECT_PYTHON" -m pytest -q'
```

The command returns authoritative queue and execution state as JSON. A failed or
stopped workload is still a successfully observed wait, so inspect the reported
outcome rather than relying only on the CLI exit status.

Common follow-up commands:

```bash
remote-runner monitor --project-config /path/to/.remote-runner.yaml
remote-runner wait --project-config /path/to/.remote-runner.yaml --run-id rr-... --until reportable
remote-runner tui --project-config /path/to/.remote-runner.yaml
remote-runner web --project-config /path/to/.remote-runner.yaml
remote-runner stop --project-config /path/to/.remote-runner.yaml --run-id rr-...
remote-runner retire-server --project-config /path/to/.remote-runner.yaml --server compute-a
remote-runner retire-server --project-config /path/to/.remote-runner.yaml --server compute-a --apply
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

For an automatic report in the current Codex App task, the originating turn must keep
`run --wait` or `remote-runner wait --until reportable` as an unfinished tool call.
This is an attached completion path, not a background callback:

1. The CLI reads the exact run's authoritative aggregate view.
2. While the run is not reportable, the CLI blocks in bounded controller `wait-run`
   requests keyed by the view etag. The controller returns early when that view
   changes. An unchanged timeout only renews the transport inside the CLI; it does not
   complete the tool call, start a model turn, or add another compute-server probe
   loop.
3. At the selected condition, the CLI writes one final authoritative JSON document to
   stdout and exits. State changes and unchanged long-poll timeouts are status-only
   stderr output.
4. Normal tool completion resumes the originating Codex turn. Codex can then inspect
   existing logs or synchronized artifacts and produce the final response. The App
   itself decides whether that response gets an unread indicator or notification from
   its focus and notification settings; Remote Runner neither writes App state nor
   guarantees either UI signal.

With neither `--max-wait` nor `--connection-grace`, the local wait has no duration or
continuous-controller-outage limit. Either option is an explicit escape hatch that
ends only the local wait and leaves the durable run untouched. If the originating App
turn or tool session ends, the run continues but there is no automatic detached
follow-up; reattach later with the exact run ID. Remote Runner has no detached Codex
callback, standalone App Server delivery, model heartbeat, or scheduled model/tool
polling path.

## Security And Support

Review [SECURITY.md](SECURITY.md) before deploying the controller or restricted
output-sync keys. Please use GitHub Issues for reproducible bugs and feature
requests. Security vulnerabilities should follow the private reporting process.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This project is licensed under the
Apache License 2.0.
