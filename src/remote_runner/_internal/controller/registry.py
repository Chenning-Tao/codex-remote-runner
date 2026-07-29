from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..execution_registry import (
    generate_run_id,
    load_yaml,
    sha256_bytes,
    utc_now,
    validate_current_run_id,
    write_yaml,
)
from ..experiment_contracts import normalize_run_binding
from ..output_paths import (
    normalize_absolute_output_path,
    normalize_output_relpath,
    normalize_output_root,
)
from ..output_sync import validate_config_payload
from ..result_metadata import (
    LEGACY_RESULT_INTENT,
    normalize_result_intent,
    normalize_result_tags,
)
from ..scheduling import (
    default_worker_policy,
    normalize_minimum_cores,
    normalize_queue_priority,
    normalize_worker_policy,
    normalize_workload_class,
    queue_priority_rank,
)


QUEUE_SCHEMA = 4
PREVIOUS_QUEUE_SCHEMA = 3
RELATIVE_OUTPUT_QUEUE_SCHEMA = 2
LEGACY_QUEUE_SCHEMA = 1
QUEUE_STATE_SCHEMA = 1
QUEUE_STATES = {"queued", "dispatching", "dispatched", "failed", "stopped"}
QUEUE_TERMINAL = {"dispatched", "failed", "stopped"}
PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class ControllerPaths:
    root: Path
    project_id: str
    project_root: Path
    config_path: Path
    registry_root: Path
    queue_dir: Path
    task_tombstones_dir: Path
    task_purges_dir: Path
    run_tombstones_dir: Path
    run_purges_dir: Path
    locks_dir: Path


@dataclass(frozen=True)
class ControllerSchedulerPaths:
    root: Path
    scheduler_root: Path
    leases_dir: Path
    locks_dir: Path
    drains_path: Path
    capacities_path: Path


def controller_scheduler_paths(root: Path) -> ControllerSchedulerPaths:
    resolved = root.expanduser().resolve()
    scheduler_root = resolved / "scheduler"
    return ControllerSchedulerPaths(
        root=resolved,
        scheduler_root=scheduler_root,
        leases_dir=scheduler_root / "leases",
        locks_dir=scheduler_root / "locks",
        drains_path=scheduler_root / "drained-servers.yaml",
        capacities_path=scheduler_root / "server-capacities.yaml",
    )


def controller_paths(root: Path, project_id: str) -> ControllerPaths:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError("invalid controller project id")
    resolved = root.expanduser().resolve()
    project_root = resolved / "projects" / project_id
    registry_root = project_root / ".remote-runner"
    return ControllerPaths(
        root=resolved,
        project_id=project_id,
        project_root=project_root,
        config_path=project_root / ".remote-runner.yaml",
        registry_root=registry_root,
        queue_dir=registry_root / "queue",
        task_tombstones_dir=registry_root / "task-tombstones",
        task_purges_dir=registry_root / "task-purges",
        run_tombstones_dir=registry_root / "run-tombstones",
        run_purges_dir=registry_root / "run-purges",
        locks_dir=registry_root / "locks",
    )


@contextlib.contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _queue_lock(paths: ControllerPaths) -> contextlib.AbstractContextManager[None]:
    return _lock(paths.locks_dir / "queue.lock")


def _scheduler_lock(root: Path) -> contextlib.AbstractContextManager[None]:
    paths = controller_scheduler_paths(root)
    return _lock(paths.locks_dir / "scheduler.lock")


def scheduler_lock(root: Path) -> contextlib.AbstractContextManager[None]:
    """Serialize release activation with controller-wide dispatch leases."""
    return _scheduler_lock(root)


def _capacity_slots(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1024:
        raise ValueError(f"{field} must be an integer between 0 and 1024")
    return value


def _capacity_defaults(server: dict[str, Any]) -> tuple[str, int, int]:
    name = server.get("name")
    if not isinstance(name, str) or not PROJECT_ID_RE.fullmatch(name):
        raise ValueError("server capacity default contains an invalid server name")
    standard_slots = _capacity_slots(
        server.get("standard_slots", 1),
        f"standard slots for {name!r}",
    )
    test_slots = _capacity_slots(
        server.get("test_slots", 0),
        f"test slots for {name!r}",
    )
    return name, standard_slots, test_slots


def _load_server_capacities_unlocked(
    scheduler: ControllerSchedulerPaths,
) -> dict[str, dict[str, Any]]:
    if not scheduler.capacities_path.is_file():
        return {}
    payload = load_yaml(scheduler.capacities_path)
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported server-capacity registry schema")
    servers = payload.get("servers")
    if not isinstance(servers, dict):
        raise ValueError("server-capacity registry servers must be a mapping")
    normalized: dict[str, dict[str, Any]] = {}
    for name, raw in servers.items():
        if not isinstance(name, str) or not PROJECT_ID_RE.fullmatch(name):
            raise ValueError("server-capacity registry contains an invalid server name")
        if not isinstance(raw, dict):
            raise ValueError(f"server-capacity entry for {name!r} must be a mapping")
        revision = raw.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError(f"server-capacity revision for {name!r} is invalid")
        customized = raw.get("customized", False)
        if not isinstance(customized, bool):
            raise ValueError(f"server-capacity customized flag for {name!r} is invalid")
        updated_at = raw.get("updated_at")
        if not isinstance(updated_at, str) or not updated_at:
            raise ValueError(f"server-capacity entry for {name!r} lacks updated_at")
        normalized[name] = {
            "standard_slots": _capacity_slots(
                raw.get("standard_slots"), f"standard slots for {name!r}"
            ),
            "test_slots": _capacity_slots(
                raw.get("test_slots"), f"test slots for {name!r}"
            ),
            "revision": revision,
            "customized": customized,
            "updated_at": updated_at,
            **(
                {"updated_by_project": raw["updated_by_project"]}
                if isinstance(raw.get("updated_by_project"), str)
                else {}
            ),
        }
    return normalized


def ensure_server_capacities(
    paths: ControllerPaths,
    defaults: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    parsed = [_capacity_defaults(server) for server in defaults]
    _ensure_controller_tree(paths)
    scheduler = controller_scheduler_paths(paths.root)
    _private_tree(scheduler.scheduler_root)
    with _scheduler_lock(paths.root):
        servers = _load_server_capacities_unlocked(scheduler)
        changed = False
        for name, standard_slots, test_slots in parsed:
            current = servers.get(name)
            if current is None:
                servers[name] = {
                    "standard_slots": standard_slots,
                    "test_slots": test_slots,
                    "revision": 0,
                    "customized": False,
                    "updated_at": utc_now(),
                }
                changed = True
            elif not current["customized"] and (
                current["standard_slots"] != standard_slots
                or current["test_slots"] != test_slots
            ):
                servers[name] = {
                    **current,
                    "standard_slots": standard_slots,
                    "test_slots": test_slots,
                    "revision": int(current["revision"]) + 1,
                    "updated_at": utc_now(),
                }
                changed = True
        if changed:
            write_yaml(
                scheduler.capacities_path,
                {"schema_version": 1, "servers": dict(sorted(servers.items()))},
            )
        return {name: dict(value) for name, value in servers.items()}


def list_server_capacities(paths: ControllerPaths) -> dict[str, dict[str, Any]]:
    scheduler = controller_scheduler_paths(paths.root)
    with _scheduler_lock(paths.root):
        return {
            name: dict(value)
            for name, value in _load_server_capacities_unlocked(scheduler).items()
        }


def update_server_capacity(
    paths: ControllerPaths,
    server: str,
    *,
    expected_revision: int,
    standard_slots: int,
    test_slots: int,
) -> dict[str, Any]:
    if not PROJECT_ID_RE.fullmatch(server):
        raise ValueError(f"invalid server name: {server!r}")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("server capacity expected_revision must be non-negative")
    standard_slots = _capacity_slots(standard_slots, "standard slots")
    test_slots = _capacity_slots(test_slots, "test slots")
    _ensure_controller_tree(paths)
    scheduler = controller_scheduler_paths(paths.root)
    _private_tree(scheduler.scheduler_root)
    with _scheduler_lock(paths.root):
        servers = _load_server_capacities_unlocked(scheduler)
        current = servers.get(server)
        if current is None:
            raise FileNotFoundError(f"server capacity does not exist: {server}")
        if int(current["revision"]) != expected_revision:
            raise RuntimeError("server capacity revision conflict")
        changed = (
            current["standard_slots"] != standard_slots
            or current["test_slots"] != test_slots
            or not current["customized"]
        )
        if not changed:
            return {"changed": False, "server": server, "capacity": current}
        updated = {
            "standard_slots": standard_slots,
            "test_slots": test_slots,
            "revision": expected_revision + 1,
            "customized": True,
            "updated_at": utc_now(),
            "updated_by_project": paths.project_id,
        }
        servers[server] = updated
        write_yaml(
            scheduler.capacities_path,
            {"schema_version": 1, "servers": dict(sorted(servers.items()))},
        )
        return {"changed": True, "server": server, "capacity": updated}


def run_purge_lock(
    paths: ControllerPaths,
    run_id: str,
) -> contextlib.AbstractContextManager[None]:
    validated = validate_current_run_id(run_id)
    return _lock(paths.locks_dir / f"purge-{validated}.lock")


def _private_tree(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_controller_tree(paths: ControllerPaths) -> None:
    for directory in (
        paths.root,
        paths.root / "projects",
        paths.project_root,
        paths.registry_root,
    ):
        _private_tree(directory)


def _queue_entry_dir(paths: ControllerPaths, run_id: str) -> Path:
    return paths.queue_dir / run_id


def validate_task_identity(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError("task identity must be a non-empty single-line string")
    return value


def task_identity_digest(task_id: object) -> str:
    identity = validate_task_identity(task_id)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def task_tombstone_path(paths: ControllerPaths, task_id: object) -> Path:
    if paths.task_tombstones_dir.is_symlink():
        raise ValueError("task tombstones root must not be a symlink")
    return paths.task_tombstones_dir / f"{task_identity_digest(task_id)}.yaml"


def task_purge_dir(paths: ControllerPaths, task_id: object) -> Path:
    if paths.task_purges_dir.is_symlink():
        raise ValueError("task purges root must not be a symlink")
    path = paths.task_purges_dir / task_identity_digest(task_id)
    if path.is_symlink():
        raise ValueError("task purge directory must not be a symlink")
    return path


def run_tombstone_path(paths: ControllerPaths, run_id: str) -> Path:
    validated = validate_current_run_id(run_id)
    if paths.run_tombstones_dir.is_symlink():
        raise ValueError("run tombstones root must not be a symlink")
    return paths.run_tombstones_dir / f"{validated}.yaml"


def run_purge_dir(paths: ControllerPaths, run_id: str) -> Path:
    validated = validate_current_run_id(run_id)
    if paths.run_purges_dir.is_symlink():
        raise ValueError("run purges root must not be a symlink")
    path = paths.run_purges_dir / validated
    if path.is_symlink():
        raise ValueError("run purge directory must not be a symlink")
    return path


def _load_run_tombstone_unlocked(
    paths: ControllerPaths,
    run_id: str,
) -> dict[str, Any] | None:
    validated = validate_current_run_id(run_id)
    path = run_tombstone_path(paths, validated)
    if not path.is_file():
        return None
    if path.is_symlink():
        raise ValueError(f"run tombstone must not be a symlink: {path}")
    tombstone = load_yaml(path)
    if tombstone.get("schema_version") != 1:
        raise ValueError(f"unsupported run tombstone schema: {path}")
    if tombstone.get("run_id") != validated:
        raise ValueError(f"run tombstone identity mismatch: {path}")
    validate_task_identity(tombstone.get("task_id"))
    if tombstone.get("status") not in {"purging", "purged"}:
        raise ValueError(f"invalid run tombstone status: {path}")
    policy = tombstone.get("replacement_policy")
    replacement_run_id = tombstone.get("replacement_run_id")
    if policy == "replacement":
        if not isinstance(replacement_run_id, str):
            raise ValueError(f"replacement tombstone has no run id: {path}")
        validate_current_run_id(replacement_run_id)
    elif policy == "explicit_none":
        if replacement_run_id is not None:
            raise ValueError(f"explicit-none tombstone has a replacement: {path}")
    else:
        raise ValueError(f"invalid run tombstone replacement policy: {path}")
    for field in ("target_provenance_sha256",):
        value = tombstone.get(field)
        if not isinstance(value, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", value
        ):
            raise ValueError(f"invalid run tombstone {field}: {path}")
    replacement_digest = tombstone.get("replacement_provenance_sha256")
    if policy == "replacement" and replacement_digest is None:
        raise ValueError(f"replacement tombstone has no provenance digest: {path}")
    if policy == "explicit_none" and replacement_digest is not None:
        raise ValueError(f"explicit-none tombstone has replacement provenance: {path}")
    if replacement_digest is not None and (
        not isinstance(replacement_digest, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", replacement_digest)
    ):
        raise ValueError(f"invalid replacement provenance digest: {path}")
    return tombstone


def load_run_tombstone(
    paths: ControllerPaths,
    run_id: str,
) -> dict[str, Any] | None:
    with _queue_lock(paths):
        return _load_run_tombstone_unlocked(paths, run_id)


def _purging_run_for_task_unlocked(
    paths: ControllerPaths,
    task_id: str,
) -> str | None:
    if not paths.run_tombstones_dir.is_dir():
        return None
    if paths.run_tombstones_dir.is_symlink():
        raise ValueError("run tombstones root must not be a symlink")
    for path in sorted(paths.run_tombstones_dir.glob("rr-*.yaml")):
        run_id = path.stem
        tombstone = _load_run_tombstone_unlocked(paths, run_id)
        if (
            tombstone is not None
            and tombstone["status"] == "purging"
            and tombstone["task_id"] == task_id
        ):
            return run_id
    return None


def _replacement_dependent_unlocked(
    paths: ControllerPaths,
    run_id: str,
) -> str | None:
    validated = validate_current_run_id(run_id)
    if not paths.run_tombstones_dir.is_dir():
        return None
    if paths.run_tombstones_dir.is_symlink():
        raise ValueError("run tombstones root must not be a symlink")
    for path in sorted(paths.run_tombstones_dir.glob("rr-*.yaml")):
        tombstone = _load_run_tombstone_unlocked(paths, path.stem)
        if tombstone is not None and tombstone.get("replacement_run_id") == validated:
            return str(tombstone["run_id"])
    return None


def replacement_dependent(
    paths: ControllerPaths,
    run_id: str,
) -> str | None:
    with _queue_lock(paths):
        return _replacement_dependent_unlocked(paths, run_id)


def create_run_tombstone(
    paths: ControllerPaths,
    run_id: str,
    *,
    task_id: object,
    reason: str,
    replacement_policy: str,
    replacement_run_id: str | None,
    target_provenance_sha256: str,
    replacement_provenance_sha256: str | None,
    now: str | None = None,
) -> dict[str, Any]:
    validated_run_id = validate_current_run_id(run_id)
    identity = validate_task_identity(task_id)
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or "\x00" in reason
        or "\n" in reason
        or "\r" in reason
        or len(reason) > 512
    ):
        raise ValueError("run purge reason must be a single line of at most 512 chars")
    if replacement_policy == "replacement":
        validated_replacement = validate_current_run_id(str(replacement_run_id))
        if replacement_provenance_sha256 is None:
            raise ValueError("replacement purge policy requires provenance evidence")
    elif replacement_policy == "explicit_none":
        if replacement_run_id is not None or replacement_provenance_sha256 is not None:
            raise ValueError("explicit-none purge policy cannot include a replacement")
        validated_replacement = None
    else:
        raise ValueError("invalid run purge replacement policy")
    for value in (target_provenance_sha256, replacement_provenance_sha256):
        if value is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ValueError("run purge provenance digest must be sha256")
    with _queue_lock(paths):
        existing = _load_run_tombstone_unlocked(paths, validated_run_id)
        if existing is not None:
            return existing
        if _load_task_tombstone_unlocked(paths, identity) is not None:
            raise RuntimeError("cannot purge one run while its task is purging")
        dependent = _replacement_dependent_unlocked(paths, validated_run_id)
        if dependent is not None:
            raise RuntimeError(
                f"run is retained as replacement provenance for {dependent}"
            )
        if (
            validated_replacement is not None
            and _load_run_tombstone_unlocked(paths, validated_replacement) is not None
        ):
            raise RuntimeError("replacement run is already purging or purged")
        tombstone = {
            "schema_version": 1,
            "run_id": validated_run_id,
            "task_id": identity,
            "status": "purging",
            "reason": reason,
            "replacement_policy": replacement_policy,
            "replacement_run_id": validated_replacement,
            "target_provenance_sha256": target_provenance_sha256,
            "replacement_provenance_sha256": replacement_provenance_sha256,
            "created_at": now or utc_now(),
            "completed_at": None,
            "resource_summary": None,
        }
        write_yaml(run_tombstone_path(paths, validated_run_id), tombstone)
        return tombstone


def complete_run_tombstone(
    paths: ControllerPaths,
    run_id: str,
    *,
    resource_summary: dict[str, Any],
    now: str | None = None,
) -> dict[str, Any]:
    validated = validate_current_run_id(run_id)
    with _queue_lock(paths):
        tombstone = _load_run_tombstone_unlocked(paths, validated)
        if tombstone is None:
            raise FileNotFoundError(f"run tombstone does not exist: {validated}")
        if tombstone["status"] == "purged":
            return tombstone
        updated = {
            **tombstone,
            "status": "purged",
            "completed_at": now or utc_now(),
            "resource_summary": resource_summary,
        }
        write_yaml(run_tombstone_path(paths, validated), updated)
        return updated


def _load_task_tombstone_unlocked(
    paths: ControllerPaths,
    task_id: object,
) -> dict[str, Any] | None:
    identity = validate_task_identity(task_id)
    path = task_tombstone_path(paths, identity)
    if not path.is_file():
        return None
    if path.is_symlink():
        raise ValueError(f"task tombstone must not be a symlink: {path}")
    tombstone = load_yaml(path)
    if tombstone.get("schema_version") != 1:
        raise ValueError(f"unsupported task tombstone schema: {path}")
    if tombstone.get("task_id") != identity:
        raise ValueError(f"task tombstone identity mismatch: {path}")
    return tombstone


def load_task_tombstone(
    paths: ControllerPaths,
    task_id: object,
) -> dict[str, Any] | None:
    with _queue_lock(paths):
        return _load_task_tombstone_unlocked(paths, task_id)


def is_task_tombstoned(paths: ControllerPaths, task_id: object) -> bool:
    identity = validate_task_identity(task_id)
    return task_tombstone_path(paths, identity).is_file()


def create_task_tombstone(
    paths: ControllerPaths,
    task_id: object,
    *,
    reason: str,
    now: str | None = None,
) -> dict[str, Any]:
    identity = validate_task_identity(task_id)
    if (
        not isinstance(reason, str)
        or not reason.strip()
        or "\x00" in reason
        or "\n" in reason
        or "\r" in reason
        or len(reason) > 512
    ):
        raise ValueError("task purge reason must be a single line of at most 512 chars")
    with _queue_lock(paths):
        existing = _load_task_tombstone_unlocked(paths, identity)
        if existing is not None:
            return existing
        purging_run = _purging_run_for_task_unlocked(paths, identity)
        if purging_run is not None:
            raise RuntimeError(
                f"cannot purge a task while one of its runs is purging: {purging_run}"
            )
        tombstone = {
            "schema_version": 1,
            "task_id": identity,
            "status": "purging",
            "reason": reason,
            "created_at": now or utc_now(),
            "completed_at": None,
        }
        write_yaml(task_tombstone_path(paths, identity), tombstone)
        return tombstone


def complete_task_tombstone(
    paths: ControllerPaths,
    task_id: object,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    identity = validate_task_identity(task_id)
    with _queue_lock(paths):
        tombstone = _load_task_tombstone_unlocked(paths, identity)
        if tombstone is None:
            raise FileNotFoundError(f"task tombstone does not exist: {identity}")
        updated = {
            **tombstone,
            "status": "purged",
            "completed_at": now or utc_now(),
        }
        write_yaml(task_tombstone_path(paths, identity), updated)
        return updated


def _validate_job(job: dict[str, Any]) -> dict[str, Any]:
    schema = job.get("schema_version")
    if schema not in {
        LEGACY_QUEUE_SCHEMA,
        RELATIVE_OUTPUT_QUEUE_SCHEMA,
        PREVIOUS_QUEUE_SCHEMA,
        QUEUE_SCHEMA,
    }:
        raise ValueError("unsupported queued job schema")
    for field in (
        "run_id",
        "project_id",
        "revision",
        "label",
        "task_id",
        "submitted_command",
        "submitted_command_sha256",
        "worker_arg",
        "created_at",
    ):
        value = job.get(field)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(f"queued job {field} must be a non-empty string")
    if not re.fullmatch(r"rr-[0-9a-f]{16}", str(job["run_id"])):
        raise ValueError("invalid queued run id")
    if not PROJECT_ID_RE.fullmatch(str(job["project_id"])):
        raise ValueError("invalid queued project id")
    if not re.fullmatch(r"[0-9a-f]{40}", str(job["revision"])):
        raise ValueError("queued revision must be a full Git SHA")
    if "queue_priority" not in job:
        job["queue_priority"] = "normal"
    else:
        job["queue_priority"] = normalize_queue_priority(job["queue_priority"])
    try:
        default_queue_position = int(
            datetime.fromisoformat(str(job["created_at"])).timestamp() * 1_000_000_000
        )
    except (OverflowError, ValueError):
        default_queue_position = 0
    queue_position = job.get("queue_position", default_queue_position)
    if (
        isinstance(queue_position, bool)
        or not isinstance(queue_position, int)
        or queue_position < 0
    ):
        raise ValueError("queued job queue_position must be a non-negative integer")
    job["queue_position"] = queue_position
    if "workload_class" not in job:
        job["workload_class"] = "standard"
    else:
        job["workload_class"] = normalize_workload_class(job["workload_class"])
    if "worker_policy" not in job:
        job["worker_policy"] = default_worker_policy(job["workload_class"])
    else:
        job["worker_policy"] = normalize_worker_policy(job["worker_policy"])
    if schema >= QUEUE_SCHEMA:
        if "result_intent" not in job or "result_tags" not in job:
            raise ValueError(
                "queued job result_intent and result_tags fields are required"
            )
        job["result_intent"] = normalize_result_intent(
            job["result_intent"], field="queued job result_intent"
        )
        job["result_tags"] = normalize_result_tags(
            job["result_tags"], field="queued job result_tags"
        )
    else:
        job["result_intent"] = LEGACY_RESULT_INTENT
        job["result_tags"] = {}
    if "minimum_cores" not in job:
        job["minimum_cores"] = 1
    else:
        job["minimum_cores"] = normalize_minimum_cores(job["minimum_cores"])
    minimum_cores = int(job["minimum_cores"])
    server_scope = job.get("server_scope", "snapshot")
    if server_scope not in {"snapshot", "all"}:
        raise ValueError("queued job server_scope must be 'snapshot' or 'all'")
    job["server_scope"] = server_scope
    prepared = job.get("prepared_servers")
    if not isinstance(prepared, list) or not prepared:
        raise ValueError("queued job prepared_servers must be a non-empty list")
    names: set[str] = set()
    for item in prepared:
        if not isinstance(item, dict):
            raise ValueError("queued prepared server must be a mapping")
        for field in (
            "name",
            "ssh",
            "ssh_profile",
            "bare_repo",
            "worktree_root",
            "python",
        ):
            value = item.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"queued prepared server {field} must be a string")
        name = str(item["name"])
        if name in names:
            raise ValueError(f"duplicate queued prepared server: {name}")
        names.add(name)
        cores = item.get("configured_cores")
        if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
            raise ValueError("queued prepared server configured_cores must be positive")
        if cores < minimum_cores:
            raise ValueError(
                f"queued prepared server {name!r} has fewer than "
                f"the required {minimum_cores} cores"
            )
        priority = item.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("queued prepared server priority must be an integer")
        if schema >= RELATIVE_OUTPUT_QUEUE_SCHEMA and "output_root" not in item:
            raise ValueError("queued prepared server output_root field is required")
        item["output_root"] = normalize_output_root(
            item.get("output_root"),
            "queued prepared server output_root",
        )
        test_slots = item.get("test_slots", 0)
        if (
            isinstance(test_slots, bool)
            or not isinstance(test_slots, int)
            or test_slots < 0
        ):
            raise ValueError("queued prepared server test_slots must be non-negative")
        item["test_slots"] = test_slots
    eligible = job.get("eligible_servers")
    if eligible is None:
        eligible = [str(item["name"]) for item in prepared]
    if not isinstance(eligible, list) or not eligible:
        raise ValueError("queued job eligible_servers must be a non-empty list")
    if any(not isinstance(name, str) or not name for name in eligible):
        raise ValueError("queued job eligible server names must be non-empty strings")
    if len(set(eligible)) != len(eligible):
        raise ValueError("queued job eligible_servers must not contain duplicates")
    unknown = set(eligible) - names
    if unknown:
        raise ValueError(
            "queued job eligible_servers contains an unprepared server: "
            + ", ".join(sorted(unknown))
        )
    job["eligible_servers"] = eligible
    eligible_servers_locked = job.get("eligible_servers_locked", False)
    if not isinstance(eligible_servers_locked, bool):
        raise ValueError("queued job eligible_servers_locked must be boolean")
    job["eligible_servers_locked"] = eligible_servers_locked
    command = str(job["submitted_command"])
    if job["submitted_command_sha256"] != sha256_bytes(command.encode("utf-8")):
        raise ValueError("queued submitted command digest mismatch")
    output_metadata = job.get("output_metadata")
    if not isinstance(output_metadata, dict):
        raise ValueError("queued output_metadata must be a mapping")
    output_sync = job.get("output_sync")
    if output_sync is None:
        job["output_sync"] = None
    else:
        job["output_sync"] = validate_config_payload(output_sync).to_payload()
    if schema == LEGACY_QUEUE_SCHEMA:
        output_path = job.get("output_path")
        if output_path is not None and (
            not isinstance(output_path, str) or not output_path
        ):
            raise ValueError("queued output_path must be a string or null")
        job["output_relpath"] = None
        return job

    if "output_relpath" not in job:
        raise ValueError("queued output_relpath field is required")
    raw_relpath = job.get("output_relpath")
    raw_path = job.get("output_path")
    if raw_relpath is not None and raw_path is not None:
        raise ValueError("queued output_relpath and output_path are mutually exclusive")
    output_relpath = (
        None
        if raw_relpath is None
        else normalize_output_relpath(raw_relpath, "queued output_relpath")
    )
    output_path = (
        None
        if raw_path is None
        else normalize_absolute_output_path(raw_path, "queued output_path")
    )
    if output_relpath is not None and any(
        item.get("output_root") is None for item in prepared
    ):
        raise ValueError("queued relative output requires every prepared output_root")
    if output_path is not None and len(prepared) != 1:
        raise ValueError("queued absolute output requires exactly one prepared server")
    if output_path is not None and job["server_scope"] == "all":
        raise ValueError("queued all-server job requires a portable relative output")
    job["output_relpath"] = output_relpath
    job["output_path"] = output_path
    raw_binding = job.get("experiment_binding")
    if raw_binding is None:
        job["experiment_binding"] = None
    else:
        binding = normalize_run_binding(raw_binding)
        if binding["run_id"] != job["run_id"]:
            raise ValueError("queued experiment binding run_id mismatch")
        if binding["source_revision"] != job["revision"]:
            raise ValueError("queued experiment binding source_revision mismatch")
        if binding["expects_result_manifest"]:
            if output_relpath is None or output_sync is None:
                raise ValueError(
                    "result-producing experiment binding requires synchronized output"
                )
            if job["result_intent"] != "candidate":
                raise ValueError(
                    "result-producing experiment binding requires candidate result intent"
                )
        job["experiment_binding"] = binding
    return job


def _validate_state(state: dict[str, Any], run_id: str) -> dict[str, Any]:
    if state.get("state_schema_version") != QUEUE_STATE_SCHEMA:
        raise ValueError("unsupported queued state schema")
    if state.get("run_id") != run_id:
        raise ValueError("queued state run id mismatch")
    if state.get("status") not in QUEUE_STATES:
        raise ValueError("invalid queued state")
    revision = state.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("queued state revision must be non-negative")
    placement_update = state.get("placement_update")
    if placement_update is not None:
        if not isinstance(placement_update, dict):
            raise ValueError("queued state placement_update must be a mapping")
        token_sha256 = placement_update.get("token_sha256")
        expires_at = placement_update.get("expires_at")
        requested_servers = placement_update.get("requested_servers")
        if not isinstance(token_sha256, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", token_sha256
        ):
            raise ValueError("queued state placement update token is invalid")
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or expires_at <= 0
        ):
            raise ValueError("queued state placement update expiry is invalid")
        if (
            not isinstance(requested_servers, list)
            or not requested_servers
            or any(
                not isinstance(name, str) or not PROJECT_ID_RE.fullmatch(name)
                for name in requested_servers
            )
            or len(set(requested_servers)) != len(requested_servers)
        ):
            raise ValueError("queued state placement update servers are invalid")
    return state


def submit_job(
    paths: ControllerPaths,
    job: dict[str, Any],
    *,
    now: str | None = None,
) -> Path:
    job = dict(job)
    run_id = str(job.get("run_id") or generate_run_id(runs_dir=paths.queue_dir))
    created_at = now or utc_now()
    job.update(
        {
            "schema_version": QUEUE_SCHEMA,
            "run_id": run_id,
            "project_id": paths.project_id,
            "created_at": created_at,
            "queue_position": time.time_ns(),
        }
    )
    _validate_job(job)
    state = {
        "state_schema_version": QUEUE_STATE_SCHEMA,
        "run_id": run_id,
        "revision": 0,
        "status": "queued",
        "created_at": created_at,
        "updated_at": created_at,
        "error": None,
    }
    _ensure_controller_tree(paths)
    _private_tree(paths.queue_dir)
    with _queue_lock(paths):
        tombstone = _load_task_tombstone_unlocked(paths, job["task_id"])
        if tombstone is not None:
            raise ValueError(
                f"task has been purged and cannot accept new runs: {job['task_id']}"
            )
        if _load_run_tombstone_unlocked(paths, run_id) is not None:
            raise ValueError(f"run id has been purged and cannot be reused: {run_id}")
        destination = _queue_entry_dir(paths, run_id)
        if destination.exists():
            raise FileExistsError(f"queued run already exists: {run_id}")
        temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=paths.queue_dir))
        try:
            os.chmod(temporary, 0o700)
            write_yaml(temporary / "job.yaml", job)
            write_yaml(temporary / "state.yaml", state)
            os.rename(temporary, destination)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.rmdir()
    return destination


def load_job(
    paths: ControllerPaths, run_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    entry = _queue_entry_dir(paths, run_id)
    job = _validate_job(load_yaml(entry / "job.yaml"))
    state = load_job_state(paths, run_id)
    return job, state


def load_job_state(paths: ControllerPaths, run_id: str) -> dict[str, Any]:
    entry = _queue_entry_dir(paths, run_id)
    return _validate_state(load_yaml(entry / "state.yaml"), run_id)


def list_jobs(
    paths: ControllerPaths,
    *,
    statuses: set[str] | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if not paths.queue_dir.is_dir():
        return []
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in paths.queue_dir.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            job, state = load_job(paths, entry.name)
        except (OSError, RuntimeError, ValueError):
            continue
        if statuses is None or state["status"] in statuses:
            rows.append((job, state))
    return sorted(
        rows, key=lambda item: (str(item[0]["created_at"]), str(item[0]["run_id"]))
    )


def list_queued(
    paths: ControllerPaths,
    *,
    jobs: list[tuple[dict[str, Any], dict[str, Any]]] | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    source = list_jobs(paths, statuses={"queued"}) if jobs is None else jobs
    rows = [
        row
        for row in source
        if row[1]["status"] == "queued"
        and not is_task_tombstoned(paths, row[0]["task_id"])
    ]
    return sorted(
        rows,
        key=lambda item: (
            -queue_priority_rank(item[0]["queue_priority"]),
            int(item[0]["queue_position"]),
            str(item[0]["created_at"]),
            str(item[0]["run_id"]),
        ),
    )


def placement_update_active(
    state: dict[str, Any],
    *,
    now: float | None = None,
) -> bool:
    placement_update = state.get("placement_update")
    return (
        isinstance(placement_update, dict)
        and float(placement_update.get("expires_at", 0)) > (time.time() if now is None else now)
    )


def _placement_token_matches(state: dict[str, Any], token: str) -> bool:
    placement_update = state.get("placement_update")
    return isinstance(placement_update, dict) and placement_update.get(
        "token_sha256"
    ) == sha256_bytes(token.encode("utf-8"))


def reserve_queued_job_update(
    paths: ControllerPaths,
    run_id: str,
    *,
    expected_revision: int,
    requested_servers: list[str],
    ttl_seconds: int,
    now: float | None = None,
) -> dict[str, Any]:
    validated_run_id = validate_current_run_id(run_id)
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("queue update expected_revision must be non-negative")
    if (
        not isinstance(requested_servers, list)
        or not requested_servers
        or any(
            not isinstance(name, str) or not PROJECT_ID_RE.fullmatch(name)
            for name in requested_servers
        )
        or len(set(requested_servers)) != len(requested_servers)
    ):
        raise ValueError("queue update requested_servers are invalid")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= 3600
    ):
        raise ValueError("queue update ttl_seconds must be between 1 and 3600")

    with _queue_lock(paths):
        if not _queue_entry_dir(paths, validated_run_id).is_dir():
            raise FileNotFoundError(f"queued run does not exist: {validated_run_id}")
        _job, state = load_job(paths, validated_run_id)
        if state["status"] != "queued":
            raise ValueError(
                f"queued run {validated_run_id} is {state['status']}, not editable"
            )
        if int(state["revision"]) != expected_revision:
            raise RuntimeError("queued state revision conflict")
        timestamp = time.time() if now is None else now
        if placement_update_active(state, now=timestamp):
            raise RuntimeError("queued run already has a placement update in progress")

        token = secrets.token_urlsafe(32)
        updated = {
            **state,
            "revision": expected_revision + 1,
            "updated_at": utc_now(),
            "placement_update": {
                "token_sha256": sha256_bytes(token.encode("utf-8")),
                "expires_at": timestamp + ttl_seconds,
                "requested_servers": list(requested_servers),
            },
        }
        _validate_state(updated, validated_run_id)
        write_yaml(_queue_entry_dir(paths, validated_run_id) / "state.yaml", updated)
        return {"token": token, "state": updated}


def release_queued_job_update(
    paths: ControllerPaths,
    run_id: str,
    *,
    token: str,
) -> dict[str, Any]:
    validated_run_id = validate_current_run_id(run_id)
    if not isinstance(token, str) or not token:
        raise ValueError("queue update token must be a non-empty string")
    with _queue_lock(paths):
        _job, state = load_job(paths, validated_run_id)
        if state.get("placement_update") is None:
            return {"changed": False, "state": state}
        if not _placement_token_matches(state, token):
            raise RuntimeError("queue update reservation token mismatch")
        updated = {
            key: value for key, value in state.items() if key != "placement_update"
        }
        updated.update(
            {
                "revision": int(state["revision"]) + 1,
                "updated_at": utc_now(),
            }
        )
        _validate_state(updated, validated_run_id)
        write_yaml(_queue_entry_dir(paths, validated_run_id) / "state.yaml", updated)
        return {"changed": True, "state": updated}


def eligible_prepared_servers(job: dict[str, Any]) -> list[dict[str, Any]]:
    eligible = set(
        job.get("eligible_servers")
        or [str(server["name"]) for server in job["prepared_servers"]]
    )
    return [server for server in job["prepared_servers"] if server["name"] in eligible]


def update_queued_job(
    paths: ControllerPaths,
    run_id: str,
    *,
    expected_revision: int,
    queue_priority: str | None = None,
    workload_class: str | None = None,
    eligible_servers: list[str] | None = None,
    move: str | None = None,
    placement_token: str | None = None,
) -> dict[str, Any]:
    validated_run_id = validate_current_run_id(run_id)
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("queued job expected_revision must be a non-negative integer")
    if queue_priority is not None:
        queue_priority = normalize_queue_priority(queue_priority)
    if workload_class is not None:
        workload_class = normalize_workload_class(workload_class)
    if move not in {None, "first", "up", "down"}:
        raise ValueError("queued job move must be 'first', 'up', or 'down'")
    if (
        queue_priority is None
        and workload_class is None
        and eligible_servers is None
        and move is None
    ):
        raise ValueError("queued job update does not contain any changes")

    with _queue_lock(paths):
        if not _queue_entry_dir(paths, validated_run_id).is_dir():
            raise FileNotFoundError(f"queued run does not exist: {validated_run_id}")
        job, state = load_job(paths, validated_run_id)
        if state["status"] != "queued":
            raise ValueError(
                f"queued run {validated_run_id} is {state['status']}, not editable"
            )
        if int(state["revision"]) != expected_revision:
            raise RuntimeError("queued state revision conflict")
        has_active_reservation = placement_update_active(state)
        if has_active_reservation and (
            placement_token is None or not _placement_token_matches(state, placement_token)
        ):
            raise RuntimeError("queued run has a placement update in progress")
        if placement_token is not None and not has_active_reservation:
            raise RuntimeError("queue update reservation expired")

        queued_rows = [
            row
            for row in list_jobs(paths, statuses={"queued"})
            if _load_task_tombstone_unlocked(paths, row[0]["task_id"]) is None
        ]
        updated_job = dict(job)
        changed = has_active_reservation
        ordering_changed = False

        if eligible_servers is not None:
            if not isinstance(eligible_servers, list) or not eligible_servers:
                raise ValueError("queued job eligible_servers must be a non-empty list")
            if any(not isinstance(name, str) or not name for name in eligible_servers):
                raise ValueError(
                    "queued job eligible server names must be non-empty strings"
                )
            if len(set(eligible_servers)) != len(eligible_servers):
                raise ValueError(
                    "queued job eligible_servers must not contain duplicates"
                )
            supported = {str(server["name"]) for server in job["prepared_servers"]}
            unknown = set(eligible_servers) - supported
            if unknown:
                raise ValueError(
                    "queued job eligible_servers contains an unprepared server: "
                    + ", ".join(sorted(unknown))
                )
            ordered = [
                str(server["name"])
                for server in job["prepared_servers"]
                if server["name"] in set(eligible_servers)
            ]
            if ordered != job["eligible_servers"] or not job["eligible_servers_locked"]:
                updated_job["eligible_servers"] = ordered
                updated_job["eligible_servers_locked"] = True
                changed = True

        if workload_class is not None and workload_class != job["workload_class"]:
            updated_job["workload_class"] = workload_class
            destination = [
                candidate
                for candidate, _candidate_state in queued_rows
                if candidate["run_id"] != validated_run_id
                and candidate["workload_class"] == workload_class
                and candidate["queue_priority"] == updated_job["queue_priority"]
            ]
            updated_job["queue_position"] = (
                max(
                    (int(candidate["queue_position"]) for candidate in destination),
                    default=0,
                )
                + 1024
            )
            changed = True
            ordering_changed = True

        if queue_priority is not None and queue_priority != job["queue_priority"]:
            updated_job["queue_priority"] = queue_priority
            destination = [
                candidate
                for candidate, _candidate_state in queued_rows
                if candidate["run_id"] != validated_run_id
                and candidate["workload_class"] == updated_job["workload_class"]
                and candidate["queue_priority"] == queue_priority
            ]
            updated_job["queue_position"] = (
                max(
                    (int(candidate["queue_position"]) for candidate in destination),
                    default=0,
                )
                + 1024
            )
            changed = True
            ordering_changed = True

        jobs_to_write: dict[str, dict[str, Any]] = {}
        if move is not None:
            lane = []
            for candidate, _candidate_state in queued_rows:
                current = (
                    updated_job
                    if candidate["run_id"] == validated_run_id
                    else candidate
                )
                if (
                    current["workload_class"] == updated_job["workload_class"]
                    and current["queue_priority"] == updated_job["queue_priority"]
                ):
                    lane.append(current)
            lane.sort(
                key=lambda candidate: (
                    int(candidate["queue_position"]),
                    str(candidate["created_at"]),
                    str(candidate["run_id"]),
                )
            )
            index = next(
                i
                for i, candidate in enumerate(lane)
                if candidate["run_id"] == validated_run_id
            )
            if move == "first" and index > 0:
                reordered = [lane[index], *lane[:index], *lane[index + 1 :]]
                base = min(int(candidate["queue_position"]) for candidate in lane)
                for position, candidate in enumerate(reordered):
                    positioned = {
                        **candidate,
                        "queue_position": base + position * 2,
                    }
                    jobs_to_write[str(positioned["run_id"])] = positioned
                updated_job = jobs_to_write[validated_run_id]
                changed = True
                ordering_changed = True
            destination_index = index - 1 if move == "up" else index + 1
            if move != "first" and 0 <= destination_index < len(lane):
                neighbor = lane[destination_index]
                target_position = int(updated_job["queue_position"])
                neighbor_position = int(neighbor["queue_position"])
                if target_position == neighbor_position:
                    base = min(int(candidate["queue_position"]) for candidate in lane)
                    for position, candidate in enumerate(lane):
                        positioned = {
                            **candidate,
                            "queue_position": base + position * 2,
                        }
                        jobs_to_write[str(positioned["run_id"])] = positioned
                    updated_job = jobs_to_write[validated_run_id]
                    neighbor = jobs_to_write[str(neighbor["run_id"])]
                    target_position = int(updated_job["queue_position"])
                    neighbor_position = int(neighbor["queue_position"])
                updated_job["queue_position"] = neighbor_position
                moved_neighbor = {**neighbor, "queue_position": target_position}
                jobs_to_write[str(moved_neighbor["run_id"])] = moved_neighbor
                changed = True
                ordering_changed = True

        if not changed:
            return {"changed": False, "job": job, "state": state}

        _validate_job(updated_job)
        jobs_to_write[validated_run_id] = updated_job
        for changed_run_id, changed_job in jobs_to_write.items():
            _validate_job(changed_job)
            write_yaml(
                _queue_entry_dir(paths, changed_run_id) / "job.yaml", changed_job
            )

        updated_at = utc_now()
        updated_state = state
        states_by_run_id = {
            str(candidate["run_id"]): candidate_state
            for candidate, candidate_state in queued_rows
        }
        # Ordering changes invalidate selections across the queue. Placement-only
        # edits invalidate just the changed record; otherwise a sequential batch
        # invalidates its own remaining expected revisions after its first update.
        state_run_ids = (
            [str(candidate["run_id"]) for candidate, _state in queued_rows]
            if ordering_changed
            else list(jobs_to_write)
        )
        for changed_run_id in state_run_ids:
            candidate_state = states_by_run_id[changed_run_id]
            bumped = {
                **candidate_state,
                "revision": int(candidate_state["revision"]) + 1,
                "updated_at": updated_at,
            }
            if changed_run_id == validated_run_id and has_active_reservation:
                bumped.pop("placement_update", None)
            _validate_state(bumped, changed_run_id)
            write_yaml(
                _queue_entry_dir(paths, changed_run_id) / "state.yaml",
                bumped,
            )
            if changed_run_id == validated_run_id:
                updated_state = bumped

        return {"changed": True, "job": updated_job, "state": updated_state}


def list_queued_all(paths: ControllerPaths) -> list[dict[str, Any]]:
    return [
        {
            "run_id": job["run_id"],
            "revision": job["revision"],
            "minimum_cores": job["minimum_cores"],
            "workload_class": job["workload_class"],
            "prepared_servers": [server["name"] for server in job["prepared_servers"]],
            "output_relpath": job["output_relpath"],
        }
        for job, _state in list_queued(paths)
        if job["server_scope"] == "all"
    ]


def extend_queued_all(
    paths: ControllerPaths,
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with _queue_lock(paths):
        for update in updates:
            run_id = update.get("run_id")
            revision = update.get("revision")
            additions = update.get("prepared_servers")
            if not isinstance(run_id, str) or not re.fullmatch(
                r"rr-[0-9a-f]{16}", run_id
            ):
                raise ValueError("pool update run_id is invalid")
            if not isinstance(revision, str) or not re.fullmatch(
                r"[0-9a-f]{40}", revision
            ):
                raise ValueError("pool update revision is invalid")
            if not isinstance(additions, list):
                raise ValueError("pool update prepared_servers must be a list")
            try:
                job, state = load_job(paths, run_id)
            except FileNotFoundError:
                results.append(
                    {"run_id": run_id, "status": "skipped", "reason": "missing"}
                )
                continue
            if state["status"] != "queued":
                results.append(
                    {"run_id": run_id, "status": "skipped", "reason": state["status"]}
                )
                continue
            if placement_update_active(state):
                results.append(
                    {"run_id": run_id, "status": "skipped", "reason": "updating"}
                )
                continue
            if job["server_scope"] != "all" or job["revision"] != revision:
                results.append(
                    {"run_id": run_id, "status": "skipped", "reason": "job changed"}
                )
                continue
            existing = {str(server["name"]) for server in job["prepared_servers"]}
            merged = list(job["prepared_servers"])
            for server in additions:
                if not isinstance(server, dict):
                    raise ValueError("pool update prepared server must be a mapping")
                if server.get("name") not in existing:
                    merged.append(server)
                    existing.add(str(server.get("name")))
            if len(merged) == len(job["prepared_servers"]):
                results.append({"run_id": run_id, "status": "unchanged"})
                continue
            updated = dict(job)
            updated["prepared_servers"] = merged
            if not job["eligible_servers_locked"]:
                updated["eligible_servers"] = [
                    str(server["name"]) for server in merged
                ]
            _validate_job(updated)
            write_yaml(_queue_entry_dir(paths, run_id) / "job.yaml", updated)
            results.append(
                {
                    "run_id": run_id,
                    "status": "extended",
                    "added_servers": len(merged) - len(job["prepared_servers"]),
                }
            )
    return results


def extend_queued_job(
    paths: ControllerPaths,
    run_id: str,
    *,
    revision: str,
    prepared_servers: list[dict[str, Any]],
    placement_token: str | None = None,
) -> dict[str, Any]:
    validate_current_run_id(run_id)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("queued job extension revision is invalid")
    if not prepared_servers:
        raise ValueError("queued job extension requires prepared servers")
    if any(not isinstance(server, dict) for server in prepared_servers):
        raise ValueError("queued job extension prepared server must be a mapping")

    with _queue_lock(paths):
        job, state = load_job(paths, run_id)
        if state["status"] != "queued":
            raise ValueError(
                f"queued run {run_id} is {state['status']}, not eligible for extension"
            )
        active_update = placement_update_active(state)
        if active_update and (
            placement_token is None or not _placement_token_matches(state, placement_token)
        ):
            raise RuntimeError("queued run has a placement update in progress")
        if placement_token is not None and not active_update:
            raise RuntimeError("queue update reservation expired")
        if job["revision"] != revision:
            raise ValueError(f"queued run {run_id} revision changed")
        existing = {str(server["name"]) for server in job["prepared_servers"]}
        merged = list(job["prepared_servers"])
        for server in prepared_servers:
            if server.get("name") not in existing:
                merged.append(server)
                existing.add(str(server.get("name")))
        if len(merged) == len(job["prepared_servers"]):
            return {
                "run_id": run_id,
                "status": "unchanged",
                "added_servers": 0,
                "prepared_servers": [
                    str(server["name"]) for server in job["prepared_servers"]
                ],
            }

        updated = dict(job)
        updated["prepared_servers"] = merged
        if not job["eligible_servers_locked"]:
            updated["eligible_servers"] = [str(server["name"]) for server in merged]
        _validate_job(updated)
        write_yaml(_queue_entry_dir(paths, run_id) / "job.yaml", updated)
        return {
            "run_id": run_id,
            "status": "extended",
            "added_servers": len(merged) - len(job["prepared_servers"]),
            "prepared_servers": [str(server["name"]) for server in merged],
        }


def purge_queue_entry(
    paths: ControllerPaths,
    run_id: str,
    *,
    allowed_statuses: frozenset[str] = frozenset({"stopped"}),
) -> Path:
    if not allowed_statuses or not allowed_statuses.issubset(QUEUE_TERMINAL):
        raise ValueError("queue purge statuses must be terminal")
    with _queue_lock(paths):
        _job, state = load_job(paths, run_id)
        if state["status"] not in allowed_statuses:
            raise ValueError(
                f"queue run {run_id} is {state['status']}, not eligible for purge"
            )
        source = _queue_entry_dir(paths, run_id)
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"queue run path is not a private directory: {run_id}")
        shutil.rmtree(source)
        return source


def stage_terminal_queue_entry(
    paths: ControllerPaths,
    run_id: str,
    *,
    task_id: object,
) -> Path:
    identity = validate_task_identity(task_id)
    destination = task_purge_dir(paths, identity) / "records" / run_id / "queue"
    with _queue_lock(paths):
        source = _queue_entry_dir(paths, run_id)
        if not source.exists():
            if destination.is_dir() and not destination.is_symlink():
                return destination
            raise FileNotFoundError(f"queue run does not exist: {run_id}")
        job, state = load_job(paths, run_id)
        if job["task_id"] != identity:
            raise ValueError(f"queue run {run_id} belongs to another task")
        if state["status"] not in QUEUE_TERMINAL:
            raise ValueError(
                f"queue run {run_id} is {state['status']}, not terminal for task purge"
            )
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"queue run path is not a private directory: {run_id}")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        if destination.exists():
            raise FileExistsError(f"queue purge destination already exists: {run_id}")
        os.rename(source, destination)
        _fsync_directory(paths.queue_dir)
        _fsync_directory(destination.parent)
        return destination


def stage_failed_queue_entry(
    paths: ControllerPaths,
    run_id: str,
    *,
    task_id: object,
) -> Path:
    validated = validate_current_run_id(run_id)
    identity = validate_task_identity(task_id)
    destination = run_purge_dir(paths, validated) / "records" / "queue"
    with _queue_lock(paths):
        source = _queue_entry_dir(paths, validated)
        if not source.exists():
            if destination.is_dir() and not destination.is_symlink():
                return destination
            raise FileNotFoundError(f"queue run does not exist: {validated}")
        job, state = load_job(paths, validated)
        if job["task_id"] != identity:
            raise ValueError(f"queue run {validated} belongs to another task")
        if state["status"] not in {"dispatched", "failed"}:
            raise ValueError(
                f"queue run {validated} is {state['status']}, not failed for run purge"
            )
        tombstone = _load_run_tombstone_unlocked(paths, validated)
        if tombstone is None or tombstone["status"] != "purging":
            raise ValueError(f"queue run {validated} has no active run purge")
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"queue run path is not a private directory: {validated}")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        if destination.exists():
            raise FileExistsError(
                f"run purge queue destination already exists: {validated}"
            )
        os.rename(source, destination)
        _fsync_directory(paths.queue_dir)
        _fsync_directory(destination.parent)
        return destination


def transition_queued_state(
    paths: ControllerPaths,
    run_id: str,
    *,
    expected_revision: int,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    allowed = {
        "queued": {"dispatching", "failed", "stopped"},
        "dispatching": {"dispatched", "failed", "stopped"},
        "dispatched": set(),
        "failed": set(),
        "stopped": set(),
    }
    with _queue_lock(paths):
        job, state = load_job(paths, run_id)
        if int(state["revision"]) != expected_revision:
            raise RuntimeError("queued state revision conflict")
        current = str(state["status"])
        if (
            status == "dispatching"
            and _load_task_tombstone_unlocked(paths, job["task_id"]) is not None
        ):
            raise RuntimeError("cannot dispatch a tombstoned task")
        if status != current and status not in allowed[current]:
            raise ValueError(f"illegal queued state transition {current} -> {status}")
        updated = dict(state)
        updated.update(
            {
                "revision": expected_revision + 1,
                "status": status,
                "updated_at": utc_now(),
                "error": error,
            }
        )
        _validate_state(updated, run_id)
        write_yaml(_queue_entry_dir(paths, run_id) / "state.yaml", updated)
        return updated


def recover_dispatching_state(
    paths: ControllerPaths,
    run_id: str,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    with _queue_lock(paths):
        _job, state = load_job(paths, run_id)
        if int(state["revision"]) != expected_revision:
            raise RuntimeError("queued state revision conflict")
        if state["status"] != "dispatching":
            raise ValueError("only an interrupted dispatching job can be recovered")
        updated = dict(state)
        updated.update(
            {
                "revision": expected_revision + 1,
                "status": "queued",
                "updated_at": utc_now(),
                "error": "recovered after interrupted dispatch",
            }
        )
        _validate_state(updated, run_id)
        write_yaml(_queue_entry_dir(paths, run_id) / "state.yaml", updated)
        return updated


def _load_drained_servers_unlocked(
    scheduler: ControllerSchedulerPaths,
) -> dict[str, dict[str, str]]:
    if not scheduler.drains_path.is_file():
        return {}
    payload = load_yaml(scheduler.drains_path)
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported drained-server registry schema")
    servers = payload.get("servers")
    if not isinstance(servers, dict):
        raise ValueError("drained-server registry servers must be a mapping")
    normalized: dict[str, dict[str, str]] = {}
    for name, metadata in servers.items():
        if not isinstance(name, str) or not PROJECT_ID_RE.fullmatch(name):
            raise ValueError("drained-server registry contains an invalid server name")
        if not isinstance(metadata, dict):
            raise ValueError(f"drained-server metadata for {name!r} must be a mapping")
        drained_at = metadata.get("drained_at")
        requested_by_project = metadata.get("requested_by_project")
        if not isinstance(drained_at, str) or not drained_at:
            raise ValueError(f"drained-server metadata for {name!r} lacks drained_at")
        if not isinstance(requested_by_project, str) or not PROJECT_ID_RE.fullmatch(
            requested_by_project
        ):
            raise ValueError(
                f"drained-server metadata for {name!r} has invalid project identity"
            )
        normalized[name] = {
            "drained_at": drained_at,
            "requested_by_project": requested_by_project,
        }
    return normalized


def list_drained_servers(paths: ControllerPaths) -> dict[str, dict[str, str]]:
    scheduler = controller_scheduler_paths(paths.root)
    with _scheduler_lock(paths.root):
        return _load_drained_servers_unlocked(scheduler)


def set_server_drained(
    paths: ControllerPaths,
    server: str,
    *,
    drained: bool,
) -> dict[str, Any]:
    if not PROJECT_ID_RE.fullmatch(server):
        raise ValueError(f"invalid server name: {server!r}")
    _ensure_controller_tree(paths)
    scheduler = controller_scheduler_paths(paths.root)
    _private_tree(scheduler.scheduler_root)
    with _scheduler_lock(paths.root):
        servers = _load_drained_servers_unlocked(scheduler)
        in_flight_dispatch: dict[str, Any] | None = None
        lease_path = scheduler.leases_dir / f"{server}.yaml"
        if lease_path.is_file():
            try:
                lease = load_yaml(lease_path)
                if float(lease.get("expires_at", 0)) > time.time():
                    in_flight_dispatch = {
                        "project_id": lease.get("project_id"),
                        "run_id": lease.get("run_id"),
                        "expires_at": lease.get("expires_at"),
                    }
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        changed = (server in servers) == (not drained)
        if drained:
            if changed:
                servers[server] = {
                    "drained_at": utc_now(),
                    "requested_by_project": paths.project_id,
                }
        else:
            servers.pop(server, None)
        if changed:
            write_yaml(
                scheduler.drains_path,
                {"schema_version": 1, "servers": dict(sorted(servers.items()))},
            )
        return {
            "server": server,
            "drained": drained,
            "changed": changed,
            "drained_servers": dict(sorted(servers.items())),
            "in_flight_dispatch": in_flight_dispatch,
        }


def _acquire_server_lease(
    paths: ControllerPaths,
    *,
    server: str,
    run_id: str,
    ttl_seconds: int,
    kind: str,
    allow_drained: bool,
    now: float | None = None,
) -> bool:
    if ttl_seconds <= 0:
        raise ValueError("dispatch lease TTL must be positive")
    timestamp = time.time() if now is None else now
    _ensure_controller_tree(paths)
    scheduler = controller_scheduler_paths(paths.root)
    _private_tree(scheduler.scheduler_root)
    _private_tree(scheduler.leases_dir)
    lease_path = scheduler.leases_dir / f"{server}.yaml"
    with _scheduler_lock(paths.root):
        if not allow_drained and server in _load_drained_servers_unlocked(scheduler):
            return False
        if lease_path.is_file():
            try:
                existing = load_yaml(lease_path)
                expires_at = float(existing.get("expires_at", 0))
            except (OSError, RuntimeError, TypeError, ValueError):
                expires_at = 0
            same_owner = (
                existing.get("project_id") == paths.project_id
                and existing.get("run_id") == run_id
            )
            if expires_at > timestamp and not same_owner:
                return False
        write_yaml(
            lease_path,
            {
                "server": server,
                "project_id": paths.project_id,
                "run_id": run_id,
                "kind": kind,
                "created_at": timestamp,
                "expires_at": timestamp + ttl_seconds,
            },
        )
        return True


def acquire_dispatch_lease(
    paths: ControllerPaths,
    *,
    server: str,
    run_id: str,
    ttl_seconds: int,
    now: float | None = None,
) -> bool:
    return _acquire_server_lease(
        paths,
        server=server,
        run_id=run_id,
        ttl_seconds=ttl_seconds,
        kind="dispatch",
        allow_drained=False,
        now=now,
    )


def acquire_maintenance_lease(
    paths: ControllerPaths,
    *,
    server: str,
    run_id: str,
    ttl_seconds: int,
    now: float | None = None,
) -> bool:
    return _acquire_server_lease(
        paths,
        server=server,
        run_id=run_id,
        ttl_seconds=ttl_seconds,
        kind="maintenance",
        allow_drained=True,
        now=now,
    )


def release_dispatch_lease(paths: ControllerPaths, *, server: str, run_id: str) -> bool:
    scheduler = controller_scheduler_paths(paths.root)
    lease_path = scheduler.leases_dir / f"{server}.yaml"
    with _scheduler_lock(paths.root):
        if not lease_path.is_file():
            return False
        try:
            lease = load_yaml(lease_path)
        except (OSError, RuntimeError, ValueError):
            return False
        if lease.get("project_id") != paths.project_id or lease.get("run_id") != run_id:
            return False
        lease_path.unlink()
        return True


def has_unexpired_dispatch_lease(
    paths: ControllerPaths,
    *,
    run_id: str,
    now: float | None = None,
) -> bool:
    timestamp = time.time() if now is None else now
    scheduler = controller_scheduler_paths(paths.root)
    if not scheduler.leases_dir.is_dir():
        return False
    with _scheduler_lock(paths.root):
        for lease_path in scheduler.leases_dir.glob("*.yaml"):
            try:
                lease = load_yaml(lease_path)
                expires_at = float(lease.get("expires_at", 0))
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if (
                lease.get("project_id") == paths.project_id
                and lease.get("run_id") == run_id
                and expires_at > timestamp
            ):
                return True
    return False
