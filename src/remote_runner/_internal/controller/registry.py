from __future__ import annotations

import contextlib
import fcntl
import hashlib
import math
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
    project_paths,
    registry_kind,
    sha256_bytes,
    utc_now,
    validate_current_run_id,
    write_yaml,
)
from ..output_paths import (
    normalize_absolute_output_path,
    normalize_output_relpath,
    normalize_output_root,
)
from ..machine_identity import (
    MACHINE_ID_RE,
    normalize_machine_fingerprint,
    normalize_server_identity,
)
from ..derivation import derived_run_id, spec_digest, validate_relation
from ..output_sync import validate_config_payload
from ..scheduling import (
    normalize_minimum_cores,
    normalize_queue_priority,
    normalize_requested_cores,
    normalize_workload_class,
    queue_priority_rank,
)


QUEUE_SCHEMA = 5
# Derived validation jobs use their own queue format instead of an optional field
# on the ordinary one. A runtime that predates derived runs must not be able to
# dispatch one: it would launch the validator without its frozen source identity.
DERIVED_QUEUE_SCHEMA = 6
PREVIOUS_QUEUE_SCHEMA = 4
PRIORITY_QUEUE_SCHEMA = 3
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
    machines_path: Path


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
        machines_path=scheduler_root / "machine-identities.yaml",
    )


@dataclass(frozen=True)
class LeaseOwnership:
    machine_id: str
    server: str
    project_id: str
    run_id: str
    token: str
    expires_at: float


class MalformedLeaseError(RuntimeError):
    pass


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


def _machine_alias(project_id: str, server: str) -> str:
    if not PROJECT_ID_RE.fullmatch(project_id) or not PROJECT_ID_RE.fullmatch(server):
        raise ValueError("machine alias contains an invalid project or server name")
    return f"{project_id}/{server}"


def _load_machine_identities_unlocked(
    scheduler: ControllerSchedulerPaths,
) -> dict[str, dict[str, Any]]:
    if not scheduler.machines_path.is_file():
        return {}
    payload = load_yaml(scheduler.machines_path)
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported machine-identity registry schema")
    machines = payload.get("machines")
    if not isinstance(machines, dict):
        raise ValueError("machine-identity registry machines must be a mapping")
    normalized: dict[str, dict[str, Any]] = {}
    aliases_seen: dict[str, str] = {}
    fingerprints_seen: dict[str, str] = {}
    legacy_ids_seen: dict[str, str] = {}
    for machine_id, raw in machines.items():
        if not isinstance(machine_id, str) or MACHINE_ID_RE.fullmatch(machine_id) is None:
            raise ValueError("machine-identity registry contains an invalid machine_id")
        if not isinstance(raw, dict):
            raise ValueError(f"machine identity for {machine_id!r} must be a mapping")
        fingerprint = normalize_machine_fingerprint(raw.get("machine_fingerprint"))
        cores = raw.get("configured_cores")
        if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
            raise ValueError(
                f"machine identity for {machine_id!r} has invalid configured_cores"
            )
        memory_gb = raw.get("configured_memory_gb")
        if memory_gb is not None and (
            isinstance(memory_gb, bool)
            or not isinstance(memory_gb, int)
            or memory_gb <= 0
        ):
            raise ValueError(
                f"machine identity for {machine_id!r} has invalid configured_memory_gb"
            )
        aliases = raw.get("aliases")
        if (
            not isinstance(aliases, list)
            or not aliases
            or any(
                not isinstance(alias, str)
                or alias.count("/") != 1
                or any(
                    PROJECT_ID_RE.fullmatch(part) is None
                    for part in alias.split("/", 1)
                )
                for alias in aliases
            )
            or len(set(aliases)) != len(aliases)
        ):
            raise ValueError(f"machine identity for {machine_id!r} has invalid aliases")
        for alias in aliases:
            previous = aliases_seen.setdefault(alias, machine_id)
            if previous != machine_id:
                raise ValueError(
                    f"machine alias {alias!r} is bound to multiple machine IDs"
                )
        identity_source = raw.get("identity_source")
        if identity_source is None:
            identity_source = (
                "legacy-name"
                if all(alias.rsplit("/", 1)[-1] == machine_id for alias in aliases)
                else "explicit"
            )
        if identity_source not in {"explicit", "legacy-name"}:
            raise ValueError(
                f"machine identity for {machine_id!r} has invalid identity_source"
            )
        legacy_machine_ids = raw.get("legacy_machine_ids", [])
        if (
            not isinstance(legacy_machine_ids, list)
            or any(
                not isinstance(legacy_id, str)
                or MACHINE_ID_RE.fullmatch(legacy_id) is None
                or legacy_id == machine_id
                for legacy_id in legacy_machine_ids
            )
            or len(set(legacy_machine_ids)) != len(legacy_machine_ids)
        ):
            raise ValueError(
                f"machine identity for {machine_id!r} has invalid legacy_machine_ids"
            )
        for legacy_id in legacy_machine_ids:
            previous = legacy_ids_seen.setdefault(legacy_id, machine_id)
            if previous != machine_id:
                raise ValueError(
                    f"legacy machine_id {legacy_id!r} maps to multiple machine IDs"
                )
        if fingerprint is not None:
            previous = fingerprints_seen.setdefault(fingerprint, machine_id)
            if previous != machine_id:
                raise ValueError(
                    "one physical machine fingerprint is bound to multiple machine IDs"
                )
        normalized[machine_id] = {
            "machine_fingerprint": fingerprint,
            "configured_cores": cores,
            "configured_memory_gb": memory_gb,
            "aliases": sorted(aliases),
            "identity_source": identity_source,
            "legacy_machine_ids": sorted(legacy_machine_ids),
            "updated_at": str(raw.get("updated_at") or "unknown"),
        }
    conflicts = set(normalized).intersection(legacy_ids_seen)
    if conflicts:
        raise ValueError(
            "machine-identity registry reuses canonical IDs as legacy IDs: "
            + ", ".join(sorted(conflicts))
        )
    return normalized


def _machine_alias_owner(
    machines: dict[str, dict[str, Any]], alias: str
) -> str | None:
    matches = [
        machine_id
        for machine_id, machine in machines.items()
        if alias in machine["aliases"]
    ]
    if len(matches) > 1:
        raise ValueError(f"machine alias {alias!r} is bound to multiple machine IDs")
    return matches[0] if matches else None


def _machine_fingerprint_owner(
    machines: dict[str, dict[str, Any]], fingerprint: str | None
) -> str | None:
    if fingerprint is None:
        return None
    matches = [
        machine_id
        for machine_id, machine in machines.items()
        if machine.get("machine_fingerprint") == fingerprint
    ]
    if len(matches) > 1:
        raise ValueError(
            "one physical machine fingerprint is bound to multiple machine IDs"
        )
    return matches[0] if matches else None


def _canonical_machine_id_unlocked(
    machines: dict[str, dict[str, Any]], machine_id: str
) -> str:
    if machine_id in machines:
        return machine_id
    matches = [
        canonical
        for canonical, machine in machines.items()
        if machine_id in machine.get("legacy_machine_ids", [])
    ]
    if len(matches) > 1:
        raise ValueError(f"legacy machine_id {machine_id!r} is ambiguous")
    return matches[0] if matches else machine_id


def _resolve_machine_request_unlocked(
    machines: dict[str, dict[str, Any]],
    *,
    project_id: str,
    server: str,
    machine_id: str,
) -> str:
    canonical = _canonical_machine_id_unlocked(machines, machine_id)
    alias_owner = _machine_alias_owner(machines, _machine_alias(project_id, server))
    if alias_owner is None:
        return canonical
    if canonical != alias_owner and machine_id != server:
        raise ValueError(
            f"machine_id {machine_id!r} does not match server alias {server!r}"
        )
    return alias_owner


def _validate_machine_inventory(
    machine_id: str,
    current: dict[str, Any],
    *,
    fingerprint: str | None,
    cores: int,
    memory_gb: int | None,
) -> None:
    if (
        current["machine_fingerprint"] is not None
        and fingerprint is not None
        and current["machine_fingerprint"] != fingerprint
    ):
        raise ValueError(
            f"machine_id {machine_id!r} resolved to a different physical fingerprint"
        )
    if int(current["configured_cores"]) != cores:
        raise ValueError(
            f"machine_id {machine_id!r} has conflicting configured core inventory"
        )
    current_memory = current.get("configured_memory_gb")
    if (
        current_memory is not None
        and memory_gb is not None
        and current_memory != memory_gb
    ):
        raise ValueError(
            f"machine_id {machine_id!r} has conflicting configured memory inventory"
        )


def _merge_legacy_machine(
    machines: dict[str, dict[str, Any]],
    *,
    legacy_id: str,
    target_id: str,
    fingerprint: str | None,
    cores: int,
    memory_gb: int | None,
) -> None:
    legacy = machines[legacy_id]
    if legacy.get("identity_source") != "legacy-name":
        raise ValueError(
            f"machine identity {legacy_id!r} is explicit and cannot be reassigned"
        )
    _validate_machine_inventory(
        legacy_id,
        legacy,
        fingerprint=fingerprint,
        cores=cores,
        memory_gb=memory_gb,
    )
    target = machines.get(target_id)
    if target is None:
        target = {
            **legacy,
            "identity_source": "explicit",
            "legacy_machine_ids": sorted(
                set(legacy.get("legacy_machine_ids", [])) | {legacy_id}
            ),
        }
    else:
        _validate_machine_inventory(
            target_id,
            target,
            fingerprint=fingerprint or legacy.get("machine_fingerprint"),
            cores=cores,
            memory_gb=memory_gb or legacy.get("configured_memory_gb"),
        )
        target = {
            **target,
            "machine_fingerprint": target["machine_fingerprint"]
            or legacy.get("machine_fingerprint")
            or fingerprint,
            "configured_memory_gb": target.get("configured_memory_gb")
            or legacy.get("configured_memory_gb")
            or memory_gb,
            "aliases": sorted(set(target["aliases"]) | set(legacy["aliases"])),
            "identity_source": "explicit",
            "legacy_machine_ids": sorted(
                set(target.get("legacy_machine_ids", []))
                | set(legacy.get("legacy_machine_ids", []))
                | {legacy_id}
            ),
        }
    target["updated_at"] = utc_now()
    machines[target_id] = target
    if legacy_id != target_id:
        del machines[legacy_id]


def _ensure_machine_identities_unlocked(
    scheduler: ControllerSchedulerPaths,
    *,
    project_id: str,
    servers: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    machines = _load_machine_identities_unlocked(scheduler)
    changed = False
    for raw in servers:
        server = normalize_server_identity(raw)
        name = str(server["name"])
        requested_machine_id = str(server["machine_id"])
        requested_source = str(server["machine_id_source"])
        alias = _machine_alias(project_id, name)
        fingerprint = server["machine_fingerprint"]
        cores = server.get("configured_cores")
        if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
            raise ValueError(f"configured cores for {name!r} must be positive")
        memory_gb = server.get("configured_memory_gb")
        if memory_gb is not None and (
            isinstance(memory_gb, bool)
            or not isinstance(memory_gb, int)
            or memory_gb <= 0
        ):
            raise ValueError(f"configured memory for {name!r} must be positive")

        alias_owner = _machine_alias_owner(machines, alias)
        fingerprint_owner = _machine_fingerprint_owner(machines, fingerprint)
        machine_id = requested_machine_id
        if requested_source == "explicit":
            merge_ids = {
                owner
                for owner in (alias_owner, fingerprint_owner)
                if owner is not None and owner != requested_machine_id
            }
            for legacy_id in sorted(merge_ids):
                _merge_legacy_machine(
                    machines,
                    legacy_id=legacy_id,
                    target_id=requested_machine_id,
                    fingerprint=fingerprint,
                    cores=cores,
                    memory_gb=memory_gb,
                )
                changed = True
            machine_id = requested_machine_id
        elif alias_owner is not None:
            machine_id = alias_owner
        elif fingerprint_owner is not None:
            fingerprint_machine = machines[fingerprint_owner]
            if fingerprint_machine.get("identity_source") != "explicit":
                raise ValueError(
                    f"machine fingerprint for {alias!r} is already bound to legacy "
                    f"machine_id {fingerprint_owner!r}; configure one shared machine_id"
                )
            machine_id = fingerprint_owner

        current = machines.get(machine_id)
        if current is None:
            machines[machine_id] = {
                "machine_fingerprint": fingerprint,
                "configured_cores": cores,
                "configured_memory_gb": memory_gb,
                "aliases": [alias],
                "identity_source": requested_source,
                "legacy_machine_ids": [],
                "updated_at": utc_now(),
            }
            changed = True
        else:
            _validate_machine_inventory(
                machine_id,
                current,
                fingerprint=fingerprint,
                cores=cores,
                memory_gb=memory_gb,
            )
            current_memory = current.get("configured_memory_gb")
            updated = {
                **current,
                "machine_fingerprint": current["machine_fingerprint"] or fingerprint,
                "configured_memory_gb": current_memory or memory_gb,
                "aliases": sorted(set(current["aliases"]) | {alias}),
                "identity_source": (
                    "explicit"
                    if requested_source == "explicit"
                    or current.get("identity_source") == "explicit"
                    else "legacy-name"
                ),
            }
            if updated != current:
                updated["updated_at"] = utc_now()
                machines[machine_id] = updated
                changed = True
        raw["machine_id"] = machine_id
        raw["machine_id_source"] = (
            "legacy-name"
            if machine_id == name
            and machines[machine_id].get("identity_source") == "legacy-name"
            else "explicit"
        )
        raw["machine_fingerprint"] = (
            machines[machine_id].get("machine_fingerprint") or fingerprint
        )
    if changed:
        write_yaml(
            scheduler.machines_path,
            {"schema_version": 1, "machines": dict(sorted(machines.items()))},
        )
    return machines


def ensure_server_identities(
    paths: ControllerPaths,
    servers: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    _ensure_controller_tree(paths)
    scheduler = controller_scheduler_paths(paths.root)
    _private_tree(scheduler.scheduler_root)
    with _scheduler_lock(paths.root):
        return _ensure_machine_identities_unlocked(
            scheduler,
            project_id=paths.project_id,
            servers=servers,
        )


def resolve_server_identity(
    paths: ControllerPaths,
    server: dict[str, Any],
) -> dict[str, Any]:
    resolved = dict(server)
    ensure_server_identities(paths, [resolved])
    return normalize_server_identity(resolved)


def _capacity_slots(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1024:
        raise ValueError(f"{field} must be an integer between 0 and 1024")
    return value


def _capacity_defaults(server: dict[str, Any]) -> tuple[str, str, int, int]:
    normalized = normalize_server_identity(server)
    name = normalized.get("name")
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
    return str(normalized["machine_id"]), name, standard_slots, test_slots


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


def _canonicalize_capacities_unlocked(
    machines: dict[str, dict[str, Any]],
    servers: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], bool]:
    canonical: dict[str, dict[str, Any]] = {}
    changed = False
    for stored_id, capacity in servers.items():
        machine_id = _canonical_machine_id_unlocked(machines, stored_id)
        changed = changed or machine_id != stored_id
        current = canonical.get(machine_id)
        if current is None:
            canonical[machine_id] = capacity
            continue
        if (
            current["standard_slots"] != capacity["standard_slots"]
            or current["test_slots"] != capacity["test_slots"]
        ):
            raise ValueError(
                f"legacy and canonical capacity entries conflict for {machine_id!r}"
            )
        selected = current
        if capacity["customized"] and not current["customized"]:
            selected = capacity
        elif capacity["customized"] == current["customized"] and int(
            capacity["revision"]
        ) > int(current["revision"]):
            selected = capacity
        canonical[machine_id] = selected
        changed = True
    return canonical, changed


def ensure_server_capacities(
    paths: ControllerPaths,
    defaults: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    _ensure_controller_tree(paths)
    scheduler = controller_scheduler_paths(paths.root)
    _private_tree(scheduler.scheduler_root)
    with _scheduler_lock(paths.root):
        machines = _ensure_machine_identities_unlocked(
            scheduler,
            project_id=paths.project_id,
            servers=[
                server
                for server in defaults
                if isinstance(server.get("configured_cores"), int)
                and not isinstance(server.get("configured_cores"), bool)
                and int(server["configured_cores"]) > 0
            ],
        )
        parsed = [_capacity_defaults(server) for server in defaults]
        servers, changed = _canonicalize_capacities_unlocked(
            machines,
            _load_server_capacities_unlocked(scheduler),
        )
        for machine_id, _name, standard_slots, test_slots in parsed:
            current = servers.get(machine_id)
            if current is None:
                servers[machine_id] = {
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
                servers[machine_id] = {
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
        servers, _changed = _canonicalize_capacities_unlocked(
            _load_machine_identities_unlocked(scheduler),
            _load_server_capacities_unlocked(scheduler),
        )
        return {
            name: dict(value)
            for name, value in servers.items()
        }


def update_server_capacity(
    paths: ControllerPaths,
    server: str,
    *,
    machine_id: str | None = None,
    expected_revision: int,
    standard_slots: int,
    test_slots: int,
) -> dict[str, Any]:
    if not PROJECT_ID_RE.fullmatch(server):
        raise ValueError(f"invalid server name: {server!r}")
    capacity_key = machine_id or server
    if not MACHINE_ID_RE.fullmatch(capacity_key):
        raise ValueError(f"invalid machine_id: {capacity_key!r}")
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
        machines = _load_machine_identities_unlocked(scheduler)
        capacity_key = _resolve_machine_request_unlocked(
            machines,
            project_id=paths.project_id,
            server=server,
            machine_id=capacity_key,
        )
        servers, _normalized = _canonicalize_capacities_unlocked(
            machines,
            _load_server_capacities_unlocked(scheduler),
        )
        current = servers.get(capacity_key)
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
        servers[capacity_key] = updated
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
    if tombstone.get("schema_version") not in {1, 2}:
        raise ValueError(f"unsupported run tombstone schema: {path}")
    if tombstone.get("run_id") != validated:
        raise ValueError(f"run tombstone identity mismatch: {path}")
    if tombstone.get("status") not in {"purging", "purged"}:
        raise ValueError(f"invalid run tombstone status: {path}")
    return tombstone


def load_run_tombstone(
    paths: ControllerPaths,
    run_id: str,
) -> dict[str, Any] | None:
    with _queue_lock(paths):
        return _load_run_tombstone_unlocked(paths, run_id)


def create_run_tombstone(
    paths: ControllerPaths,
    run_id: str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    validated_run_id = validate_current_run_id(run_id)
    with _queue_lock(paths):
        existing = _load_run_tombstone_unlocked(paths, validated_run_id)
        if existing is not None:
            return existing
        tombstone = {
            "schema_version": 2,
            "run_id": validated_run_id,
            "status": "purging",
            "created_at": now or utc_now(),
            "completed_at": None,
        }
        write_yaml(run_tombstone_path(paths, validated_run_id), tombstone)
        return tombstone


def complete_run_tombstone(
    paths: ControllerPaths,
    run_id: str,
    *,
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
        }
        write_yaml(run_tombstone_path(paths, validated), updated)
        return updated


def _validate_job(job: dict[str, Any]) -> dict[str, Any]:
    schema = job.get("schema_version")
    if schema not in {
        LEGACY_QUEUE_SCHEMA,
        RELATIVE_OUTPUT_QUEUE_SCHEMA,
        PRIORITY_QUEUE_SCHEMA,
        PREVIOUS_QUEUE_SCHEMA,
        QUEUE_SCHEMA,
        DERIVED_QUEUE_SCHEMA,
    }:
        raise ValueError("unsupported queued job schema")
    derivation = job.get("derivation")
    if schema == DERIVED_QUEUE_SCHEMA:
        if derivation is None:
            raise ValueError("derived queued job requires a derivation relation")
        job["derivation"] = validate_relation(derivation)
    elif derivation is not None:
        raise ValueError("only derived queued jobs may carry a derivation relation")
    for field in (
        "run_id",
        "project_id",
        "revision",
        "label",
        "task_id",
        "submitted_command",
        "submitted_command_sha256",
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
    if schema == DERIVED_QUEUE_SCHEMA and job["workload_class"] != "test":
        raise ValueError("derived queued job must use the test workload class")
    if "minimum_cores" not in job:
        job["minimum_cores"] = 1
    else:
        job["minimum_cores"] = normalize_minimum_cores(job["minimum_cores"])
    minimum_cores = int(job["minimum_cores"])
    requested_cores = normalize_requested_cores(job.get("requested_cores"))
    job["requested_cores"] = requested_cores
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
        identity = normalize_server_identity(item)
        item.update(
            machine_id=identity["machine_id"],
            machine_id_source=identity["machine_id_source"],
            machine_fingerprint=identity["machine_fingerprint"],
        )
        memory_gb = item.get("configured_memory_gb")
        if memory_gb is not None and (
            isinstance(memory_gb, bool)
            or not isinstance(memory_gb, int)
            or memory_gb <= 0
        ):
            raise ValueError(
                "queued prepared server configured_memory_gb must be positive or null"
            )
        item["configured_memory_gb"] = memory_gb
        cores = item.get("configured_cores")
        if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
            raise ValueError("queued prepared server configured_cores must be positive")
        required_cores = max(minimum_cores, requested_cores or 1)
        if cores < required_cores:
            raise ValueError(
                f"queued prepared server {name!r} has fewer than "
                f"the required {required_cores} cores"
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
    if isinstance(job.get("prepared_servers"), list):
        job["prepared_servers"] = [
            dict(server) if isinstance(server, dict) else server
            for server in job["prepared_servers"]
        ]
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


def _derived_spec_sha256(job: dict[str, Any]) -> str:
    return spec_digest(
        job["derivation"],
        label=str(job["label"]),
        task_id=str(job["task_id"]),
        submitted_command_sha256=str(job["submitted_command_sha256"]),
        minimum_cores=int(job["minimum_cores"]),
        requested_cores=job.get("requested_cores"),
        workload_class=str(job["workload_class"]),
        output_relpath=str(job["output_relpath"]),
        privacy=job.get("privacy"),
        eligible_servers=[str(name) for name in job["eligible_servers"]],
    )


def ensure_derived_job(
    paths: ControllerPaths,
    job: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Submit or reuse the one validator run a derivation identity may own.

    `submit_job` cannot express this: it stamps the ordinary queue schema, always
    takes a fresh queue position, and treats an existing entry as an error instead
    of as the answer. The run ID is recomputed here from the relation, so a client
    can only confirm the identity it was given, never choose one.
    """
    job = dict(job)
    if isinstance(job.get("prepared_servers"), list):
        job["prepared_servers"] = [
            dict(server) if isinstance(server, dict) else server
            for server in job["prepared_servers"]
        ]
    relation = validate_relation(job.get("derivation"))
    job["derivation"] = relation
    run_id = derived_run_id(
        project_id=paths.project_id,
        source_run_id=relation["source_run_id"],
        validator_key=relation["validator_key"],
    )
    submitted_run_id = job.get("run_id")
    if submitted_run_id is not None and str(submitted_run_id) != run_id:
        raise ValueError(
            f"submitted validator run id {submitted_run_id!r} does not match the "
            f"derived identity {run_id!r}"
        )
    created_at = now or utc_now()
    job.update(
        {
            "schema_version": DERIVED_QUEUE_SCHEMA,
            "run_id": run_id,
            "project_id": paths.project_id,
            "created_at": created_at,
            "queue_position": time.time_ns(),
        }
    )
    _validate_job(job)
    recomputed = _derived_spec_sha256(job)
    if recomputed != relation["spec_sha256"]:
        raise ValueError(
            "submitted validator spec digest does not match the submitted job"
        )
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
        if _load_run_tombstone_unlocked(paths, run_id) is not None:
            raise ValueError(f"run id has been purged and cannot be reused: {run_id}")
        destination = _queue_entry_dir(paths, run_id)
        if destination.exists():
            existing_job, existing_state = load_job(paths, run_id)
            existing = validate_relation(existing_job.get("derivation"))
            # Comparing relations is enough: each side's spec digest was recomputed
            # from its own job before it was written, so equal relations mean equal
            # commands, resources, output paths, and placement.
            if existing != relation:
                raise ValueError(
                    f"validator run {run_id} already exists with a different "
                    "immutable spec; submit the changed validator under a new key"
                )
            return {
                "disposition": "reused",
                "run_id": run_id,
                "queue_entry": str(destination),
                "queue_status": existing_state["status"],
                "derivation": existing,
            }
        temporary = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=paths.queue_dir))
        try:
            os.chmod(temporary, 0o700)
            write_yaml(temporary / "job.yaml", job)
            write_yaml(temporary / "state.yaml", state)
            os.rename(temporary, destination)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.rmdir()
        return {
            "disposition": "created",
            "run_id": run_id,
            "queue_entry": str(destination),
            "queue_status": state["status"],
            "derivation": relation,
        }


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

        queued_rows = list_jobs(paths, statuses={"queued"})
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
        _job, state = load_job(paths, run_id)
        if int(state["revision"]) != expected_revision:
            raise RuntimeError("queued state revision conflict")
        current = str(state["status"])
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


def _canonicalize_drains_unlocked(
    machines: dict[str, dict[str, Any]],
    servers: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, str]], bool]:
    canonical: dict[str, dict[str, str]] = {}
    changed = False
    for stored_id, metadata in servers.items():
        machine_id = _canonical_machine_id_unlocked(machines, stored_id)
        changed = changed or machine_id != stored_id or machine_id in canonical
        current = canonical.get(machine_id)
        if current is None or metadata["drained_at"] < current["drained_at"]:
            canonical[machine_id] = metadata
    return canonical, changed


def list_drained_servers(paths: ControllerPaths) -> dict[str, dict[str, str]]:
    scheduler = controller_scheduler_paths(paths.root)
    with _scheduler_lock(paths.root):
        servers, _changed = _canonicalize_drains_unlocked(
            _load_machine_identities_unlocked(scheduler),
            _load_drained_servers_unlocked(scheduler),
        )
        return servers


def set_server_drained(
    paths: ControllerPaths,
    server: str,
    *,
    machine_id: str | None = None,
    drained: bool,
) -> dict[str, Any]:
    if not PROJECT_ID_RE.fullmatch(server):
        raise ValueError(f"invalid server name: {server!r}")
    requested_machine_id = machine_id or server
    if not MACHINE_ID_RE.fullmatch(requested_machine_id):
        raise ValueError(f"invalid machine_id: {requested_machine_id!r}")
    _ensure_controller_tree(paths)
    scheduler = controller_scheduler_paths(paths.root)
    _private_tree(scheduler.scheduler_root)
    with _scheduler_lock(paths.root):
        machines = _load_machine_identities_unlocked(scheduler)
        drain_key = _resolve_machine_request_unlocked(
            machines,
            project_id=paths.project_id,
            server=server,
            machine_id=requested_machine_id,
        )
        servers, normalized = _canonicalize_drains_unlocked(
            machines,
            _load_drained_servers_unlocked(scheduler),
        )
        in_flight_dispatch: dict[str, Any] | None = None
        for lease_path in _lease_paths_for_machine_unlocked(
            scheduler, machines, drain_key
        ):
            if not lease_path.is_file():
                continue
            lease = load_server_lease(lease_path)
            if float(lease["expires_at"]) > time.time() or lease["kind"] == "dispatch":
                in_flight_dispatch = {
                    "project_id": lease["project_id"],
                    "run_id": lease["run_id"],
                    "expires_at": lease["expires_at"],
                }
                break
        changed = (drain_key in servers) == (not drained)
        if drained:
            if changed:
                servers[drain_key] = {
                    "drained_at": utc_now(),
                    "requested_by_project": paths.project_id,
                }
        else:
            servers.pop(drain_key, None)
        if changed or normalized:
            write_yaml(
                scheduler.drains_path,
                {"schema_version": 1, "servers": dict(sorted(servers.items()))},
            )
        return {
            "server": server,
            "machine_id": drain_key,
            "drained": drained,
            "changed": changed,
            "drained_servers": dict(sorted(servers.items())),
            "in_flight_dispatch": in_flight_dispatch,
        }


def _lease_path(scheduler: ControllerSchedulerPaths, machine_id: str) -> Path:
    if MACHINE_ID_RE.fullmatch(machine_id) is None:
        raise ValueError(f"invalid lease machine_id: {machine_id!r}")
    return scheduler.leases_dir / f"{machine_id}.yaml"


def _lease_paths_for_machine_unlocked(
    scheduler: ControllerSchedulerPaths,
    machines: dict[str, dict[str, Any]],
    machine_id: str,
) -> list[Path]:
    canonical = _canonical_machine_id_unlocked(machines, machine_id)
    aliases = machines.get(canonical, {}).get("legacy_machine_ids", [])
    return [
        _lease_path(scheduler, candidate)
        for candidate in dict.fromkeys([canonical, *aliases])
    ]


def load_server_lease(lease_path: Path) -> dict[str, Any]:
    try:
        lease = load_yaml(lease_path)
        schema = lease.get("schema_version", 1)
        if isinstance(schema, bool) or not isinstance(schema, int) or schema not in {1, 2}:
            raise ValueError("unsupported schema_version")
        server = lease.get("server")
        project_id = lease.get("project_id")
        run_id = lease.get("run_id")
        kind = lease.get("kind")
        machine_id = lease.get("machine_id", server)
        if not isinstance(server, str) or PROJECT_ID_RE.fullmatch(server) is None:
            raise ValueError("invalid server")
        if not isinstance(machine_id, str) or MACHINE_ID_RE.fullmatch(machine_id) is None:
            raise ValueError("invalid machine_id")
        if lease_path.stem != machine_id:
            raise ValueError("lease filename does not match machine_id")
        if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
            raise ValueError("invalid project_id")
        if not isinstance(run_id, str) or not run_id or "\x00" in run_id:
            raise ValueError("invalid run_id")
        if kind not in {"dispatch", "maintenance"}:
            raise ValueError("invalid lease kind")
        raw_created_at = lease["created_at"]
        raw_expires_at = lease["expires_at"]
        if (
            isinstance(raw_created_at, bool)
            or not isinstance(raw_created_at, (int, float))
            or isinstance(raw_expires_at, bool)
            or not isinstance(raw_expires_at, (int, float))
        ):
            raise ValueError("invalid lease timestamp types")
        created_at = float(raw_created_at)
        expires_at = float(raw_expires_at)
        if (
            created_at <= 0
            or expires_at <= created_at
            or not math.isfinite(created_at)
            or not math.isfinite(expires_at)
        ):
            raise ValueError("invalid lease timestamps")
        token = lease.get("owner_token")
        if schema == 2 and (
            not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None
        ):
            raise ValueError("invalid owner_token")
        if schema == 1:
            token = None
            heartbeat_at = None
        else:
            raw_heartbeat_at = lease.get("heartbeat_at")
            if (
                isinstance(raw_heartbeat_at, bool)
                or not isinstance(raw_heartbeat_at, (int, float))
            ):
                raise ValueError("invalid heartbeat_at")
            heartbeat_at = float(raw_heartbeat_at)
            if (
                not math.isfinite(heartbeat_at)
                or not created_at <= heartbeat_at < expires_at
            ):
                raise ValueError("invalid heartbeat_at")
    except (KeyError, OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise MalformedLeaseError(f"malformed dispatch lease {lease_path}: {exc}") from exc
    return {
        **lease,
        "schema_version": schema,
        "server": server,
        "machine_id": machine_id,
        "project_id": project_id,
        "run_id": run_id,
        "kind": kind,
        "created_at": created_at,
        "expires_at": expires_at,
        "heartbeat_at": heartbeat_at,
        "owner_token": token,
    }


def dispatch_lease_authority_gone(
    paths: ControllerPaths,
    *,
    project_id: str,
    run_id: str,
    kind: str = "dispatch",
) -> bool:
    """Report whether a dispatch lease's owning project and run records are gone.

    A lease whose queue record and execution record no longer exist can be
    released safely: purge only removes terminal execution records, and a
    terminal record means no authorized live workload remains on the machine.
    Unknown-launch outcomes keep a non-terminal ``registered`` execution
    record, so this rule never converts transport ambiguity into release
    authority. Any resolution failure fails closed (authority still present).
    """

    if kind != "dispatch":
        return False
    try:
        owner_paths = controller_paths(paths.root, project_id)
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        queue_gone = not _queue_entry_dir(owner_paths, run_id).is_dir()
    except (OSError, RuntimeError, ValueError):
        return False
    if not queue_gone:
        return False
    if not owner_paths.config_path.is_file():
        return True
    try:
        return registry_kind(project_paths(owner_paths.config_path), run_id) is None
    except (OSError, RuntimeError, ValueError):
        return False


def _acquire_server_lease(
    paths: ControllerPaths,
    *,
    server: str,
    machine_id: str | None,
    run_id: str,
    ttl_seconds: int,
    kind: str,
    allow_drained: bool,
    now: float | None = None,
) -> LeaseOwnership | None:
    if ttl_seconds <= 0:
        raise ValueError("dispatch lease TTL must be positive")
    timestamp = time.time() if now is None else now
    _ensure_controller_tree(paths)
    scheduler = controller_scheduler_paths(paths.root)
    _private_tree(scheduler.scheduler_root)
    _private_tree(scheduler.leases_dir)
    with _scheduler_lock(paths.root):
        machines = _load_machine_identities_unlocked(scheduler)
        lease_machine_id = _resolve_machine_request_unlocked(
            machines,
            project_id=paths.project_id,
            server=server,
            machine_id=machine_id or server,
        )
        lease_path = _lease_path(scheduler, lease_machine_id)
        if (
            not allow_drained
            and lease_machine_id
            in _canonicalize_drains_unlocked(
                machines,
                _load_drained_servers_unlocked(scheduler),
            )[0]
        ):
            return None
        stale_paths: list[Path] = []
        for existing_path in _lease_paths_for_machine_unlocked(
            scheduler, machines, lease_machine_id
        ):
            if not existing_path.is_file():
                continue
            existing = load_server_lease(existing_path)
            expires_at = float(existing["expires_at"])
            same_owner = (
                existing.get("project_id") == paths.project_id
                and existing.get("run_id") == run_id
            )
            durable_dispatch = existing["kind"] == "dispatch"
            if expires_at > timestamp:
                return None
            if (
                durable_dispatch
                and not same_owner
                and not dispatch_lease_authority_gone(
                    paths,
                    project_id=str(existing["project_id"]),
                    run_id=str(existing["run_id"]),
                    kind=str(existing["kind"]),
                )
            ):
                return None
            stale_paths.append(existing_path)
        for stale_path in stale_paths:
            stale_path.unlink()
        token = secrets.token_hex(32)
        expires_at = timestamp + ttl_seconds
        write_yaml(
            lease_path,
            {
                "schema_version": 2,
                "server": server,
                "machine_id": lease_machine_id,
                "project_id": paths.project_id,
                "run_id": run_id,
                "kind": kind,
                "owner_token": token,
                "created_at": timestamp,
                "heartbeat_at": timestamp,
                "expires_at": expires_at,
            },
        )
        return LeaseOwnership(
            machine_id=lease_machine_id,
            server=server,
            project_id=paths.project_id,
            run_id=run_id,
            token=token,
            expires_at=expires_at,
        )


def acquire_dispatch_lease(
    paths: ControllerPaths,
    *,
    server: str,
    machine_id: str | None = None,
    run_id: str,
    ttl_seconds: int,
    now: float | None = None,
) -> LeaseOwnership | None:
    return _acquire_server_lease(
        paths,
        server=server,
        machine_id=machine_id,
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
    machine_id: str | None = None,
    run_id: str,
    ttl_seconds: int,
    now: float | None = None,
) -> LeaseOwnership | None:
    return _acquire_server_lease(
        paths,
        server=server,
        machine_id=machine_id,
        run_id=run_id,
        ttl_seconds=ttl_seconds,
        kind="maintenance",
        allow_drained=True,
        now=now,
    )


def renew_dispatch_lease(
    paths: ControllerPaths,
    ownership: LeaseOwnership,
    *,
    ttl_seconds: int,
    now: float | None = None,
) -> LeaseOwnership | None:
    if ttl_seconds <= 0:
        raise ValueError("dispatch lease TTL must be positive")
    timestamp = time.time() if now is None else now
    scheduler = controller_scheduler_paths(paths.root)
    lease_path = _lease_path(scheduler, ownership.machine_id)
    with _scheduler_lock(paths.root):
        if not lease_path.is_file():
            return None
        lease = load_server_lease(lease_path)
        if (
            lease["schema_version"] != 2
            or lease["kind"] != "dispatch"
            or lease["project_id"] != paths.project_id
            or lease["run_id"] != ownership.run_id
            or lease["owner_token"] != ownership.token
            or lease["machine_id"] != ownership.machine_id
        ):
            return None
        expires_at = timestamp + ttl_seconds
        write_yaml(
            lease_path,
            {
                **lease,
                "heartbeat_at": timestamp,
                "expires_at": expires_at,
            },
        )
        return LeaseOwnership(
            machine_id=ownership.machine_id,
            server=ownership.server,
            project_id=ownership.project_id,
            run_id=ownership.run_id,
            token=ownership.token,
            expires_at=expires_at,
        )


def release_dispatch_lease(
    paths: ControllerPaths,
    *,
    server: str,
    run_id: str,
    machine_id: str | None = None,
    owner_token: str | None = None,
) -> bool:
    scheduler = controller_scheduler_paths(paths.root)
    with _scheduler_lock(paths.root):
        machines = _load_machine_identities_unlocked(scheduler)
        canonical = _resolve_machine_request_unlocked(
            machines,
            project_id=paths.project_id,
            server=server,
            machine_id=machine_id or server,
        )
        matching: list[Path] = []
        for lease_path in _lease_paths_for_machine_unlocked(
            scheduler, machines, canonical
        ):
            if not lease_path.is_file():
                continue
            lease = load_server_lease(lease_path)
            if (
                lease.get("project_id") != paths.project_id
                or lease.get("run_id") != run_id
            ):
                continue
            if lease["schema_version"] == 2:
                if owner_token is None or lease.get("owner_token") != owner_token:
                    continue
            elif owner_token is not None:
                continue
            matching.append(lease_path)
        if not matching:
            return False
        if len(matching) != 1:
            raise RuntimeError(
                f"multiple matching leases exist for machine_id {canonical!r}"
            )
        matching[0].unlink()
        return True


def list_owned_dispatch_leases(paths: ControllerPaths) -> list[dict[str, Any]]:
    scheduler = controller_scheduler_paths(paths.root)
    if not scheduler.leases_dir.is_dir():
        return []
    with _scheduler_lock(paths.root):
        machines = _load_machine_identities_unlocked(scheduler)
        leases = [
            load_server_lease(lease_path)
            for lease_path in sorted(scheduler.leases_dir.glob("*.yaml"))
        ]
        owned = []
        for lease in leases:
            if lease["kind"] != "dispatch" or lease["project_id"] != paths.project_id:
                continue
            canonical = _resolve_machine_request_unlocked(
                machines,
                project_id=paths.project_id,
                server=str(lease["server"]),
                machine_id=str(lease["machine_id"]),
            )
            owned.append({**lease, "machine_id": canonical})
        return owned


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
                lease = load_server_lease(lease_path)
                expires_at = float(lease["expires_at"])
            except FileNotFoundError:
                continue
            if (
                lease.get("project_id") == paths.project_id
                and lease.get("run_id") == run_id
                and expires_at > timestamp
            ):
                return True
    return False
