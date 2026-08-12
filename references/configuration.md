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

Keep connection endpoints, configured cores and memory, placement priority, and
global enablement in `~/.codex/remote-servers.yaml`. Keep project execution ownership
in one `.remote-runner.yaml`.

Task-slot capacity belongs to the physical server and is shared by every project
under the controller root. The global registry supplies the initial test value:

```yaml
servers:
  compute-a:
    machine_id: compute-a-physical
    ssh: compute-a
    cores: 256
    memory_gb: 512
    testing:
      slots: 1
```

`machine_id` is the stable controller-wide physical identity. Aliases in different
projects must use the same `machine_id`; different machines that reuse one display
name must use different IDs. Preparation records a hashed OS machine fingerprint.
The controller rejects a machine ID that changes fingerprint, a fingerprint bound
to multiple IDs, or a project/server alias reassigned to another ID. Registries that
omit `machine_id` remain compatible by using the server name, but this legacy path
cannot safely express aliases and should be migrated when a server is next prepared.

`memory_gb` is optional static inventory metadata used by the web dashboard for
display and sorting. It does not affect admission. Live memory telemetry is also
observational only; operators enabling shared core allocations must budget memory
separately.

Live server snapshots additionally expose optional physical-memory probe fields:
`memory_total_bytes`, `memory_available_bytes`, `memory_used_bytes`, and
`memory_used_percent`. The controller reads these from the remote host during
the normal server probe, so a host without readable memory telemetry remains
usable and reports the unavailable fields as `null`.

The controller initially uses one standard slot and the configured
`testing.slots` value. The local web dashboard can then persist both values in
the controller-wide `scheduler/server-capacities.yaml` registry. Web overrides
remain authoritative across projects and later dashboard refreshes.

Standard and test slots limit concurrent runs in their respective lanes. Both lanes
also consume one shared physical core budget. Runs submitted without `--cores`
retain the compatible whole-machine allocation and therefore remain exclusive
across both lanes. `--cores N` opts into a consumable allocation of exactly `N`
cores; the controller admits a run only when both its lane slot and the shared core
budget are available. Zero disables new dispatch in that lane. Lowering a limit
below current occupancy lets existing runs finish and blocks later dispatch until
occupancy falls below the new limit. Memory does not participate in this admission.

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
  target_root: /srv/archive/example/artifacts-v1
  source_ssh_config: /srv/archive/.ssh/output-sync.conf
  source_hosts:
    compute-a: compute-a-int
  prune_after_sync:
    servers:
      - compute-a
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
retirement, preview and then apply the guarded retirement flow:

```bash
remote-runner retire-server \
  --project-config /path/to/.remote-runner.yaml \
  --server-registry ~/.codex/remote-servers.yaml \
  --server burst

remote-runner retire-server \
  --project-config /path/to/.remote-runner.yaml \
  --server-registry ~/.codex/remote-servers.yaml \
  --server burst \
  --apply
```

The dry run asks the controller to assess every project under the same controller
root. It blocks retirement for active executions, frozen queued candidates, active
server processes, pending output synchronization, unverified succeeded outputs,
unknown project state, or an unreachable server. Failed and stopped output paths are
reported separately because they are not authoritative successful results but would
be lost if the machine is destroyed.

Apply repeats the assessment after committing the controller-wide drain. It then
removes the project remote, test-lane reference, output-sync source and pruning
reference, global inventory entry, exact local SSH Host block, and matching local
known-host records. When output synchronization uses a remote source alias, apply
revokes its public key from the source before removing the archive target's exact
Host block, exclusive IdentityFile pair, and matching known-host records. Identity
files referenced by another archive Host block and local login identities are always
preserved. Controller drain and historical run records remain; runtime directories
and output data are not deleted by this command.

Retirement is also blocked when the server is the output-sync target or the
project's last enabled automatic candidate. Move those responsibilities first.
Because global registration, controller admission, SSH authorization, and archive
credentials can affect more than one project, review the complete dry-run assessment
and cleanup inventory before `--apply`.

If an instance has already been shut down, add `--allow-unreachable` explicitly.
The command still requires every other assessment to pass. If the source public key
cannot be revoked, it removes the corresponding exclusive archive private key so the
remaining authorization cannot be used and reports that degraded cleanup outcome.

If the instance was destroyed before a nonterminal run completed its normal stop
handshake, close each exact run with the preview-first
`remote-runner close-decommissioned-run` lifecycle before retiring its configuration.
This path requires explicit operator attestation and a fresh unreachable probe; it
does not infer termination from a missing configuration entry.

After adding and provisioning an automatic project remote, run
`remote-runner sync-pool --project-config /path/to/.remote-runner.yaml`. This
prepares the queued revisions for tasks submitted with `--server all` and makes
the new server eligible without resubmitting those tasks.

## Scheduling And Test Lanes

`scheduling.testing.servers` binds durable development tests to one or more
enabled project remotes. Candidates may be excluded from automatic standard
placement. Every prepared testing candidate must configure positive global
`testing.slots`.

Canonical `machine_id` values from the global server registry identify
controller-wide capacity, drains, and dispatch leases. Display names remain
project-local labels. Project queues remain isolated, while dispatch leases are
shared by all projects under one controller root so aliases cannot launch onto the
same physical machine concurrently. Keep `project_id` unique within that controller
root.
Standard and test runs consume their respective controller-wide slots independently,
but their core allocations share one physical budget. Dispatch reads the current
controller capacity instead of the historical slot values frozen into queued server
descriptors. An active legacy runtime without allocation metadata conservatively
consumes the entire core inventory until it finishes.

## Output Synchronization

`output_sync` enables default archival of every terminal run that declares an
output path, including failed or stopped checkpoints. `target_ssh` is the controller host's SSH destination for the archive
server. `source_ssh_config` and `source_hosts` are interpreted on the target server;
they must name direct target-to-source routes for every enabled non-target remote
that configures `output_root`. The target server itself is local and must not appear
in `source_hosts`. A disabled configured remote may retain its source alias while
in-flight or historical outputs still need archival; this does not make the remote
eligible for execution.

Run `remote-runner sync-outputs` once after adding or changing this section. Every
later submission carries the same validated configuration to the controller. The
controller writes one durable outbox intent when a run becomes terminal, while a
separate controller worker asks the target to pull only that run's output. Sync
outcome never changes run authority or interprets transferred bytes. Source data is
retained by default. `prune_after_sync.servers` is
an explicit allow-list of configured source hosts whose outputs may be deleted only
after checksum-verified archival; do not infer this policy from server names. The
worker also reconciles eligible completed receipts when this allow-list is enabled.
Initial synchronization configuration is forward-only and does not archive older
terminal history.

When activating the release that removes the experiment subsystem, the controller
upgrades existing schema-1 pending intents under the output-sync worker lock. The
upgrade accepts only intents whose exact run ID, succeeded execution state, state
revision, source server/path, output metadata, and terminal timestamp match the
durable run record. It discards retired result and experiment fields rather than
translating them into transport authority. Any mismatch blocks activation and leaves
the original intent unchanged.

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

Set `paused: true` during another writer's one-time migration. Terminal
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

The maintained activation flow requires the local client CLI to match the release
artifact, discovers the controller's uv tool bin, and transactionally binds its
global `remote-runner` command to the private `runner/current` runtime under the
same lease/state gate. The activation receipt reports the client, controller-global,
and controller-private revisions; a mismatch is an activation failure, not a warning.

Controller activation stops both project dispatchers and output-sync workers after
checking controller-wide dispatch leases. For the experiment-boundary migration it
atomically moves each legacy `.remote-runner/experiments` directory to private
`<controller-root>/retired-state/experiment-registry-v1/<project-id>` storage. The
move is content-opaque and idempotent. A simultaneous legacy source and retired
destination, a symlink, or an unverifiable pending transfer blocks activation rather
than merging or deleting bytes.
The migration holds the retired registry's own legacy file lock while moving it and
leaves only a private marker at the old path. That marker blocks an in-flight old
binary from recreating the removed registry and is not part of any normal query.

Official local/controller support is macOS and Linux. Do not select an
OS-specific local executable directory; uv owns tool discovery. Windows is not
currently supported.

Do not add `controller.python` or `controller.skill_root`. Remove those obsolete
fields when migrating an older project config.
