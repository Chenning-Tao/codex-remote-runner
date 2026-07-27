from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .execution_registry import load_yaml
from .output_sync import OutputSyncConfig, validate_config_payload
from .output_paths import normalize_output_root


PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEFAULT_WORKER_ARGUMENT = "--num-workers"
DEFAULT_LEASE_SECONDS = 120
DEFAULT_PROBE_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class ControllerConfig:
    ssh: str
    root: str


@dataclass(frozen=True)
class RemoteRuntime:
    name: str
    enabled: bool
    auto_select: bool
    bare_repo: str
    worktree_root: str
    python: str
    output_root: str | None

    def worktree_for_revision(self, revision: str) -> str:
        return str(PurePosixPath(self.worktree_root) / revision)


@dataclass(frozen=True)
class ParallelismConfig:
    default_arg: str


@dataclass(frozen=True)
class SchedulingConfig:
    strategy: str
    lease_seconds: int
    probe_interval_seconds: int
    testing_servers: tuple[str, ...]


@dataclass(frozen=True)
class ManagedProjectConfig:
    path: Path
    project_root: Path
    project_id: str
    local_repo: Path
    controller: ControllerConfig
    remotes: dict[str, RemoteRuntime]
    parallelism: ParallelismConfig
    scheduling: SchedulingConfig
    output_sync: OutputSyncConfig | None

    def candidate_names(
        self,
        explicit_server: str | None = None,
        candidate_servers: tuple[str, ...] | None = None,
    ) -> list[str]:
        if explicit_server is not None:
            runtime = self.remotes.get(explicit_server)
            if runtime is None:
                raise ValueError(
                    f"server {explicit_server!r} is not configured for this project"
                )
            if not runtime.enabled:
                raise ValueError(
                    f"server {explicit_server!r} is disabled for this project"
                )
            return [explicit_server]
        if candidate_servers is not None:
            candidates: list[str] = []
            for name in candidate_servers:
                runtime = self.remotes.get(name)
                if runtime is None:
                    raise ValueError(
                        f"server {name!r} is not configured for this project"
                    )
                if not runtime.enabled:
                    raise ValueError(f"server {name!r} is disabled for this project")
                candidates.append(name)
            return candidates
        return sorted(
            name
            for name, runtime in self.remotes.items()
            if runtime.enabled and runtime.auto_select
        )


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"project config {field} must be a mapping")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"project config {field} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"project config {field} must be a single-line string")
    return value


def _absolute_posix(value: Any, field: str) -> str:
    text = _text(value, field)
    if not PurePosixPath(text).is_absolute():
        raise ValueError(f"project config {field} must be an absolute POSIX path")
    return text


def _boolean(value: Any, field: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"project config {field} must be boolean")
    return value


def _positive_int(value: Any, field: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"project config {field} must be a positive integer")
    return value


def load_managed_project_config(path: Path) -> ManagedProjectConfig:
    resolved = path.expanduser().resolve()
    raw = load_yaml(resolved)
    project_root = resolved.parent

    controller = _mapping(raw.get("controller"), "controller")
    removed_controller_fields = sorted(
        field for field in ("python", "skill_root") if field in controller
    )
    if removed_controller_fields:
        fields = ", ".join(f"controller.{field}" for field in removed_controller_fields)
        raise ValueError(
            f"project config {fields} are no longer supported; remove them because "
            "remote-runner owns the controller package and interpreter under "
            "controller.root/runner"
        )
    controller_ssh = _text(controller.get("ssh"), "controller.ssh")
    controller_root = _absolute_posix(controller.get("root"), "controller.root")
    controller_config = ControllerConfig(
        ssh=controller_ssh,
        root=controller_root,
    )

    source = _mapping(raw.get("source"), "source")
    if "mode" in source:
        mode = _text(source.get("mode"), "source.mode")
        if mode != "git-worktree":
            raise ValueError("only source.mode='git-worktree' is supported")
    local_repo_value = _text(source.get("local_repo"), "source.local_repo")
    local_repo = Path(local_repo_value).expanduser()
    if not local_repo.is_absolute():
        local_repo = project_root / local_repo
    local_repo = local_repo.resolve()

    project_id = raw.get("project_id", project_root.name)
    project_id = _text(project_id, "project_id")
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError(
            "project config project_id must start with an alphanumeric character and "
            "contain only letters, digits, dots, underscores, or hyphens"
        )

    remotes_raw = _mapping(raw.get("remote"), "remote")
    if not remotes_raw:
        raise ValueError("project config remote must contain at least one server")
    remotes: dict[str, RemoteRuntime] = {}
    for name, value in remotes_raw.items():
        if not isinstance(name, str) or not PROJECT_ID_RE.fullmatch(name):
            raise ValueError(f"invalid project server name: {name!r}")
        runtime = _mapping(value, f"remote.{name}")
        if "workdir" in runtime:
            raise ValueError(
                f"project config remote.{name}.workdir is no longer supported; "
                "configure bare_repo and worktree_root"
            )
        remotes[name] = RemoteRuntime(
            name=name,
            enabled=_boolean(
                runtime.get("enabled"), f"remote.{name}.enabled", default=True
            ),
            auto_select=_boolean(
                runtime.get("auto_select"),
                f"remote.{name}.auto_select",
                default=True,
            ),
            bare_repo=_absolute_posix(
                runtime.get("bare_repo"), f"remote.{name}.bare_repo"
            ),
            worktree_root=_absolute_posix(
                runtime.get("worktree_root"),
                f"remote.{name}.worktree_root",
            ),
            python=_absolute_posix(runtime.get("python"), f"remote.{name}.python"),
            output_root=normalize_output_root(
                runtime.get("output_root"),
                f"project config remote.{name}.output_root",
            ),
        )

    if not any(runtime.enabled and runtime.auto_select for runtime in remotes.values()):
        raise ValueError("project config must enable at least one automatic candidate")

    parallelism_raw = raw.get("parallelism", {})
    parallelism = _mapping(parallelism_raw, "parallelism")
    default_value = parallelism.get("default_value", "selected_server.cores")
    if default_value != "selected_server.cores":
        raise ValueError(
            "project config parallelism.default_value must be selected_server.cores"
        )
    parallelism_config = ParallelismConfig(
        default_arg=_text(
            parallelism.get("default_arg", DEFAULT_WORKER_ARGUMENT),
            "parallelism.default_arg",
        )
    )

    scheduling_raw = raw.get("scheduling", {})
    scheduling = _mapping(scheduling_raw, "scheduling")
    strategy = scheduling.get("strategy", "max_available_cores")
    if strategy != "max_available_cores":
        raise ValueError(
            "project config scheduling.strategy must be max_available_cores"
        )
    testing_raw = scheduling.get("testing")
    testing_servers: tuple[str, ...] = ()
    if testing_raw is not None:
        testing = _mapping(testing_raw, "scheduling.testing")
        raw_servers = testing.get("servers")
        if not isinstance(raw_servers, list) or not raw_servers:
            raise ValueError(
                "project config scheduling.testing.servers must be a non-empty list"
            )
        testing_servers = tuple(
            _text(value, "scheduling.testing.servers") for value in raw_servers
        )
        if len(set(testing_servers)) != len(testing_servers):
            raise ValueError(
                "project config scheduling.testing.servers must not contain duplicates"
            )
        for testing_server in testing_servers:
            testing_runtime = remotes.get(testing_server)
            if testing_runtime is None:
                raise ValueError(
                    "project config scheduling.testing servers must name configured remotes"
                )
            if not testing_runtime.enabled:
                raise ValueError(
                    "project config scheduling.testing servers must name enabled remotes"
                )
    scheduling_config = SchedulingConfig(
        strategy=strategy,
        lease_seconds=_positive_int(
            scheduling.get("lease_seconds"),
            "scheduling.lease_seconds",
            default=DEFAULT_LEASE_SECONDS,
        ),
        probe_interval_seconds=_positive_int(
            scheduling.get("probe_interval_seconds"),
            "scheduling.probe_interval_seconds",
            default=DEFAULT_PROBE_INTERVAL_SECONDS,
        ),
        testing_servers=testing_servers,
    )

    output_sync_raw = raw.get("output_sync")
    output_sync_config: OutputSyncConfig | None = None
    if output_sync_raw is not None:
        output_sync = _mapping(output_sync_raw, "output_sync")
        target_server = _text(
            output_sync.get("target_server"), "output_sync.target_server"
        )
        target_runtime = remotes.get(target_server)
        if target_runtime is None or not target_runtime.enabled:
            raise ValueError(
                "project config output_sync.target_server must name an enabled remote"
            )
        if target_runtime.output_root is None:
            raise ValueError(
                "project config output_sync.target_server must configure output_root"
            )
        target_root = _absolute_posix(
            output_sync.get("target_root"), "output_sync.target_root"
        )
        output_root_path = PurePosixPath(target_runtime.output_root)
        target_root_path = PurePosixPath(target_root)
        if (
            target_root_path != output_root_path
            and output_root_path not in target_root_path.parents
        ):
            raise ValueError(
                "project config output_sync.target_root must be inside the target "
                "server output_root"
            )
        source_hosts_raw = _mapping(
            output_sync.get("source_hosts"), "output_sync.source_hosts"
        )
        expected_sources = {
            name
            for name, runtime in remotes.items()
            if runtime.enabled
            and runtime.output_root is not None
            and name != target_server
        }
        configured_sources = {
            name
            for name, runtime in remotes.items()
            if runtime.output_root is not None and name != target_server
        }
        actual_sources = set(source_hosts_raw)
        if (
            not expected_sources <= actual_sources
            or not actual_sources <= configured_sources
        ):
            missing = sorted(expected_sources - actual_sources)
            unknown = sorted(actual_sources - configured_sources)
            details = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if unknown:
                details.append("unknown: " + ", ".join(unknown))
            raise ValueError(
                "project config output_sync.source_hosts must cover every enabled "
                "non-target output remote and name only configured output remotes ("
                + "; ".join(details)
                + ")"
            )
        retry_seconds = _positive_int(
            output_sync.get("retry_seconds"),
            "output_sync.retry_seconds",
            default=60,
        )
        prune_after_sync_raw = output_sync.get("prune_after_sync", {})
        prune_after_sync = _mapping(
            prune_after_sync_raw, "output_sync.prune_after_sync"
        )
        prune_servers_raw = prune_after_sync.get("servers", [])
        if not isinstance(prune_servers_raw, list):
            raise ValueError(
                "project config output_sync.prune_after_sync.servers must be a list"
            )
        output_sync_config = validate_config_payload(
            {
                "schema_version": 1,
                "target_server": target_server,
                "target_ssh": _text(
                    output_sync.get("target_ssh"), "output_sync.target_ssh"
                ),
                "target_root": target_root,
                "target_python": target_runtime.python,
                "source_ssh_config": _absolute_posix(
                    output_sync.get("source_ssh_config"),
                    "output_sync.source_ssh_config",
                ),
                "source_hosts": source_hosts_raw,
                "prune_after_sync": {"servers": prune_servers_raw},
                "restricted_source_keys": _boolean(
                    output_sync.get("restricted_source_keys"),
                    "output_sync.restricted_source_keys",
                    default=False,
                ),
                "retry_seconds": retry_seconds,
                "paused": _boolean(
                    output_sync.get("paused"),
                    "output_sync.paused",
                    default=False,
                ),
            }
        )

    return ManagedProjectConfig(
        path=resolved,
        project_root=project_root,
        project_id=project_id,
        local_repo=local_repo,
        controller=controller_config,
        remotes=remotes,
        parallelism=parallelism_config,
        scheduling=scheduling_config,
        output_sync=output_sync_config,
    )
