# Configuration

Read this reference only when configuring or provisioning a project, controller,
or compute server.

## Contents

- [Registry And Project Ownership](#registry-and-project-ownership)
- [Project Configuration](#project-configuration)
- [Eligibility And Maintenance](#eligibility-and-maintenance)
- [Scheduling And Test Lanes](#scheduling-and-test-lanes)
- [Output Synchronization](#output-synchronization)
- [Restricted Source Access](#restricted-source-access)
- [Provisioning](#provisioning)

## Registry And Project Ownership

Keep connection endpoints, configured cores, placement priority, and global
enablement in `~/.codex/remote-servers.yaml`. Keep project execution ownership
in one `.remote-runner.yaml`.

Testing capacity belongs to the physical server and is shared by every project
under the controller root:

```yaml
servers:
  compute-a:
    ssh: compute-a
    cores: 256
    testing:
      slots: 1
```

`testing.slots` limits concurrent test-class runs, not cores or workers. A
positive value enables the test lane; omission disables it for that server.

## Project Configuration

The project controller config contains only its SSH alias and absolute state
root. remote-runner owns the package and interpreter underneath that root.

```yaml
project_id: example

controller:
  ssh: controller.example
  root: /Users/user/.remote-runner

source:
  mode: git-worktree
  local_repo: code

remote:
  compute-a:
    enabled: true
    auto_select: true
    bare_repo: /srv/example/repo.git
    worktree_root: /srv/example/worktrees
    python: /srv/envs/example/bin/python3
    output_root: /srv/example
  archive:
    enabled: true
    auto_select: false
    bare_repo: /srv/example/repo.git
    worktree_root: /srv/example/worktrees
    python: /srv/envs/example/bin/python3
    output_root: /home/other-user/example

parallelism:
  default_arg: --num-workers
  default_value: selected_server.cores

scheduling:
  strategy: max_available_cores
  lease_seconds: 120
  probe_interval_seconds: 60
  testing:
    servers:
      - compute-a

output_sync:
  target_server: archive
  target_ssh: archive
  target_root: /srv/archive/example/scientific-v1
  source_ssh_config: /srv/archive/.ssh/output-sync.conf
  source_hosts:
    compute-a: compute-a-int
  restricted_source_keys: true
  retry_seconds: 60
  paused: false
```

## Eligibility And Maintenance

`enabled: false` forbids all use. `auto_select: false` excludes a server from
automatic placement but permits explicit selection. Never infer capabilities
from server names.

Configuration changes govern preparation of new submissions. To keep frozen queued
candidate snapshots from dispatching to a server during maintenance or retirement,
install a controller drain:

```bash
remote-runner drain-server \
  --project-config /path/to/.remote-runner.yaml \
  --server burst
```

Drains are controller-wide because dispatch capacity is shared across projects. They
block new dispatch leases without stopping work already running on the server. The
state is persistent and appears in the project overview under `server_drains`. Run
`remote-runner resume-server --server burst` to remove it. For permanent
retirement, retain the drain and set both project and global `enabled` fields to
`false`; this also prevents unnecessary future preparation.

After adding and provisioning an automatic project remote, run
`remote-runner sync-pool --project-config /path/to/.remote-runner.yaml`. This
prepares the queued revisions for tasks submitted with `--server all` and makes
the new server eligible without resubmitting those tasks.

## Scheduling And Test Lanes

`scheduling.testing.servers` binds durable development tests to one or more
enabled project remotes. Candidates may be excluded from automatic standard
placement. Every prepared testing candidate must configure positive global
`testing.slots`.

Server names come from the global server registry and identify controller-wide
capacity. Project queues remain isolated, while dispatch leases are shared by
all projects under one controller root so two projects cannot launch onto the
same server concurrently. Keep `project_id` unique within that controller root.
Standard runs exclude other standard runs on a server. Test runs ignore standard
occupancy and consume only the server-wide test slots; running tests likewise do
not prevent a standard run from starting.

## Output Synchronization

`output_sync` enables default archival of every succeeded run that declares an
output path. `target_ssh` is the controller host's SSH destination for the archive
server. `source_ssh_config` and `source_hosts` are interpreted on the target server;
they must name direct target-to-source routes for every enabled non-target remote
that configures `output_root`. The target server itself is local and must not appear
in `source_hosts`. A disabled configured remote may retain its source alias while
in-flight or historical outputs still need archival; this does not make the remote
eligible for execution.

Run `remote-runner sync-outputs` once after adding or changing this section. Every
later submission carries the same validated configuration to the controller. The
controller writes one durable outbox intent when a run becomes succeeded, while a
separate controller worker asks the target to pull only that run's output. Sync
failures never change run authority, source data is never deleted, and canonical
scientific selection remains a separate concern. Initial configuration is
forward-only and does not backfill older succeeded history.

## Restricted Source Access

Set `restricted_source_keys: true` when every remote source alias uses a forced
command that permits only the fixed source-kind probe, the fixed artifact-identity
probe, and a read-only rsync sender within that source's configured project root.
The identity probe exposes only `COMPLETE` presence and SHA-256 digests for
`summary.json` plus an explicitly selected `manifest.json` or `config.json`.
This mode leaves remote paths visible to the forced command instead of using rsync
protected arguments. Install
`remote_runner._internal.output_source_gate` as a standalone script on each source,
and bind each source-specific public key to it with OpenSSH `restrict,command=...`.
The default is `false` for compatibility with ordinary SSH source keys.

Set `paused: true` during another writer's one-time migration. Succeeded
transitions still enter the durable outbox, but the target worker does not start.
Change it to `false` and rerun `remote-runner sync-outputs` to drain the backlog.

## Provisioning

Provision before ordinary lifecycle use:

- install the local `remote-runner` tool through the maintained release flow;
- configure controller SSH access and its private runner release;
- create each compute server's bare repository and external worktree root;
- provision each configured absolute project Python and its dependencies;
- create external output roots with appropriate ownership;
- install rsync and direct source SSH access on the output-sync target;
- install tmux on the controller and compute servers.

Official local/controller support is macOS and Linux. Do not select an
OS-specific local executable directory; uv owns tool discovery. Windows is not
currently supported.

Do not add `controller.python` or `controller.skill_root`. Remove those obsolete
fields when migrating an older project config.
