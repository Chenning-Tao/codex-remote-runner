# Codex Remote Runner

[简体中文](README.zh-CN.md)

Codex Remote Runner is a command-line application for submitting durable work to
a project-owned pool of remote machines. It keeps queue and execution state on a
controller host, runs an exact clean Git revision, and lets clients reconnect to
monitor, wait for, stop, or archive a run without depending on the original shell.
It also provides a separate foreground `dev` command for quickly testing a filtered
dirty working tree on one trusted compute server without entering that durable
lifecycle.

The project is currently pre-1.0. State formats and deployment workflows are
tested, but operators should review upgrades before applying them to active pools.

## What It Provides

- Durable queued and running workloads backed by controller-owned state.
- Automatic placement using configured capacity, availability, and priority.
- Exact Git revision preparation in detached remote worktrees.
- Detached durable submission by default, with explicit attached waits when requested.
- Local browser dashboard with confirmed run stopping.
- Opaque workload commands with selected resources exposed through `RR_ASSIGNED_CORES`.
- Explicit stop, cleanup, purge, server drain/retirement, and output archival workflows.
- Direct foreground development tests from dirty, untracked, or non-Git source trees.

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
- rsync on the local and selected compute host for `dev`, and when output
  synchronization is enabled.
- Key-based, non-interactive SSH aliases for every configured connection.

Windows is not currently supported. The application executes operator-supplied
commands on remote machines and is intended for trusted project infrastructure,
not as a hostile multi-tenant scheduler.

## Install

Install the current release directly from its GitHub tag with `uv`:

```bash
uv tool install 'codex-remote-runner[web] @ git+https://github.com/Chenning-Tao/codex-remote-runner.git@v0.9.4'
remote-runner --help
```

To install from a checkout instead:

```bash
git clone https://github.com/Chenning-Tao/codex-remote-runner.git
cd codex-remote-runner
uv tool install '.[web]'
remote-runner --help
```

For development:

```bash
uv sync --frozen --group dev
npm ci --prefix web
npm run build --prefix web
uv run pytest -q
```

The `web` extra is optional. Core lifecycle commands only require PyYAML.

Activating this boundary-changing controller release performs a one-time state
migration while dispatch leases are blocked and controller workers are stopped.
Expired dispatch leases whose owning queue and execution records are both gone are
released first, since purge only removes terminal execution records and no
authorized live workload can remain; every other lease still blocks activation.
Legacy experiment-registry bytes move atomically out of active project state to
`<controller-root>/retired-state/experiment-registry-v1/<project-id>`. Pending
schema-1 output-sync intents are rewritten to the transport-only schema only after
their run ID, terminal execution record, revision, server, path, and timestamp match.
The migration is idempotent and refuses symlinks or conflicting source/destination
state rather than overwriting history.
The active registry retains only a private retirement marker so an in-flight old
binary cannot recreate the removed subsystem; no normal controller API reads it.

## Configure

Remote Runner uses two YAML files:

1. `~/.codex/remote-servers.yaml` describes stable `machine_id` values, shared
   physical capacity, SSH endpoints, and an optional per-server `dev_root`.
2. A project-owned `.remote-runner.yaml` describes the controller, source
   repository, project remotes, scheduling, and optional output archival.

Start with [examples/remote-servers.yaml](examples/remote-servers.yaml) and
[examples/project.remote-runner.yaml](examples/project.remote-runner.yaml). See
[references/configuration.md](references/configuration.md) for the complete
contract and provisioning requirements.

For example, enable direct development runs on one server with
`dev_root: /srv/remote-runner-dev`. A project may then add `dev.include`,
`dev.exclude`, and `dev.stale_after_seconds`; see the linked examples for the minimal
schema.

## Web Dashboard

Open the dashboard for one configured project:

```bash
remote-runner web \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/worktree
```

The command binds only to `127.0.0.1`, opens the system browser, and streams the
controller snapshot. Server rows include live load and
physical-memory usage when the remote host exposes it. Use `--no-open` to leave
the browser closed or `--port PORT` to select another local port. The browser
never receives SSH configuration. The details drawer can stop one exact queued or running
workload, change a queued workload's priority and eligible servers, and prepare
its exact revision on a newly selected compatible server before enabling it. It
can also switch queued work between the standard and test lanes and edit each
server's controller-wide standard/test concurrency limits. Slot limits and the
shared physical core budget both control admission; memory is telemetry only.
Omitting `--cores` keeps the compatible whole-machine allocation, while
`--cores N` opts into sharing exactly `N` cores across both lanes. These settings
do not rewrite worker counts or stop running work. Workload class changes preserve
the submitted command unchanged.
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
## Run

Remote Runner accepts only a clean committed source revision. Submission is detached
by default and completes after the controller returns the run ID and queue record:

```bash
remote-runner run \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --source-repo /absolute/path/to/clean/worktree \
  --label "smoke test" \
  --task-id "validation/smoke" \
  --command '"$RR_PROJECT_PYTHON" -m pytest -q'
```

The workload command is stored and executed unchanged. `RR_ASSIGNED_CORES` exposes
the selected allocation; the workload decides how to use it. Add `--wait --until
reportable` only for an explicitly requested foreground wait.

Output synchronization proves path identity, transferred bytes, checksum verification,
and receipt identity. It can archive failed or stopped checkpoints and never rewrites
execution state or interprets scientific validity.

## Development Run

Use `dev` for a disposable foreground test of the current working tree:

```bash
remote-runner dev \
  --project-config /absolute/path/to/.remote-runner.yaml \
  --server compute-a \
  --command 'python3 -m pytest -q'
```

`dev` resolves `source.local_repo` unless `--source-root` supplies another absolute
directory. For Git roots it sends current tracked-file bytes plus non-ignored
untracked files; ignored files require an explicit `dev.include`. For non-Git roots
it performs a filtered filesystem walk. VCS/tool state, virtual environments,
dependency trees, build/results directories, and common credential files are excluded
by default, with structural exclusions remaining non-overridable.

Each invocation creates a fresh private
`<dev_root>/<project_id>/tmp/dev-.../source` directory and rsyncs only the selected
file list. It therefore does not resend excluded trees such as `node_modules` or
results, but it is a complete filtered snapshot rather than an incremental persistent
source checkout. The session directory is removed after success, failure, or handled
interruption. `<dev_root>/<project_id>/cache` remains and may contain source-derived
data; secure erase is not claimed.

The command inherits workload stdout/stderr and returns its exit status. It creates no
formal run ID, queue record, Web entry, output sync, or scientific provenance. It also
acquires no controller lease: `RR_ASSIGNED_CORES` and `RR_SERVER_CORES` both expose the
registered server core count, so selecting a busy server can contend with durable
runs. `MAKEFLAGS`, `CMAKE_BUILD_PARALLEL_LEVEL`, and `CARGO_BUILD_JOBS` default to all
registered cores unless explicitly set locally; the opaque command is never rewritten.

Common follow-up commands:

```bash
remote-runner monitor --project-config /path/to/.remote-runner.yaml
remote-runner wait --project-config /path/to/.remote-runner.yaml --run-id rr-... --until reportable
remote-runner web --project-config /path/to/.remote-runner.yaml
remote-runner stop --project-config /path/to/.remote-runner.yaml --run-id rr-...
remote-runner close-decommissioned-run --project-config /path/to/.remote-runner.yaml --run-id rr-... --server compute-a --reason "provider destroyed the instance"
remote-runner close-decommissioned-run --project-config /path/to/.remote-runner.yaml --run-id rr-... --server compute-a --reason "provider destroyed the instance" --apply
remote-runner retire-server --project-config /path/to/.remote-runner.yaml --server compute-a
remote-runner retire-server --project-config /path/to/.remote-runner.yaml --server compute-a --apply
```

Read [references/submission.md](references/submission.md) before changing placement,
priority, privacy, or output identity. Read
[references/lifecycle.md](references/lifecycle.md) before destructive lifecycle
operations.

## Codex Integration

[SKILL.md](SKILL.md) and [agents/openai.yaml](agents/openai.yaml) provide the Codex
skill metadata and operating contract. They complement the CLI; the Python wheel
does not install user-specific Codex configuration.

For an explicitly requested automatic report in the current Codex App task, the originating turn must keep
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

Persist exact run IDs and cross-session decisions in Trellis when needed. Do not copy
Remote Runner queue or execution records into Trellis; query the authoritative run ID.

## Security And Support

Review [SECURITY.md](SECURITY.md) before deploying the controller or restricted
output-sync keys. Please use GitHub Issues for reproducible bugs and feature
requests. Security vulnerabilities should follow the private reporting process.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). This project is licensed under the
Apache License 2.0.
