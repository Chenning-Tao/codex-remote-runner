from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..._revision import SOURCE_REVISION
from ..execution_registry import load_yaml, project_paths
from ..output_sync import migrate_legacy_pending_intents, output_sync_paths
from ..tmux import (
    dispatcher_tmux_session,
    exact_tmux_target,
    output_sync_tmux_session,
    resolve_tmux_executable,
)
from .registry import (
    ControllerPaths,
    controller_paths,
    controller_scheduler_paths,
    scheduler_lock,
)


REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
RETIRED_EXPERIMENT_MARKER_SCHEMA = 1


@dataclass(frozen=True)
class ActiveLease:
    project_id: str
    server: str
    run_id: str
    expires_at: float


def _project_ids(controller_root: Path) -> list[str]:
    projects_root = controller_root / "projects"
    if not projects_root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in projects_root.iterdir()
        if entry.is_dir() and not entry.is_symlink()
    )


def _active_leases(
    controller_root: Path,
    *,
    now: float | None = None,
) -> list[ActiveLease]:
    timestamp = time.time() if now is None else now
    active: list[ActiveLease] = []

    scheduler = controller_scheduler_paths(controller_root)
    if scheduler.leases_dir.is_dir():
        for lease_path in sorted(scheduler.leases_dir.glob("*.yaml")):
            try:
                lease = load_yaml(lease_path)
                expires_at = float(lease.get("expires_at", 0))
                project_id = str(lease["project_id"])
            except (KeyError, OSError, TypeError, ValueError):
                continue
            if expires_at <= timestamp:
                continue
            active.append(
                ActiveLease(
                    project_id=project_id,
                    server=lease_path.stem,
                    run_id=str(lease.get("run_id", "unknown")),
                    expires_at=expires_at,
                )
            )

    return active


def inspect_release_gate(controller_root: Path, *, now: float | None = None) -> dict[str, Any]:
    root = controller_root.expanduser().resolve()
    project_ids = _project_ids(root)
    leases = _active_leases(root, now=now)
    return {
        "revision": SOURCE_REVISION,
        "projects": project_ids,
        "active_leases": [lease.__dict__ for lease in leases],
    }


def _stop_controller_workers(project_ids: list[str]) -> dict[str, list[str]]:
    if not project_ids:
        return {"dispatchers": [], "output_sync_workers": []}
    tmux = resolve_tmux_executable()
    stopped = {"dispatchers": [], "output_sync_workers": []}
    for project_id in project_ids:
        for kind, session in (
            ("dispatchers", dispatcher_tmux_session(project_id)),
            ("output_sync_workers", output_sync_tmux_session(project_id)),
        ):
            target = exact_tmux_target(session)
            exists = subprocess.run(
                [tmux, "has-session", "-t", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if exists.returncode != 0:
                continue
            stopped_result = subprocess.run(
                [tmux, "kill-session", "-t", target],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if stopped_result.returncode != 0:
                raise RuntimeError(
                    stopped_result.stderr.strip()
                    or f"failed to stop controller worker {session}"
                )
            stopped[kind].append(project_id)
    return stopped


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"controller migration path must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _legacy_experiment_lock(source: Path) -> Iterator[None]:
    locks = source / "locks"
    _private_directory(locks)
    descriptor = os.open(
        locks / "registry.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _retired_experiment_marker(destination: Path) -> dict[str, Any]:
    return {
        "schema_version": RETIRED_EXPERIMENT_MARKER_SCHEMA,
        "status": "retired",
        "destination": str(destination),
    }


def _write_retired_experiment_marker(source: Path, destination: Path) -> None:
    payload = (
        json.dumps(
            _retired_experiment_marker(destination),
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    descriptor = os.open(
        source,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise OSError("write returned zero bytes")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(source.parent)


def _validate_retired_experiment_marker(source: Path, destination: Path) -> None:
    try:
        marker = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("retired experiment marker is invalid") from exc
    if marker != _retired_experiment_marker(destination):
        raise ValueError("retired experiment marker does not match its destination")


def _archive_legacy_experiment_registry(paths: ControllerPaths) -> dict[str, Any]:
    source = paths.registry_root / "experiments"
    archive_root = paths.root / "retired-state" / "experiment-registry-v1"
    destination = archive_root / paths.project_id
    if paths.registry_root.is_symlink():
        raise ValueError("controller registry root must not be a symlink")
    if source.is_symlink():
        raise ValueError("legacy experiment registry must not be a symlink")
    if destination.is_symlink():
        raise ValueError("retired experiment registry must not be a symlink")
    if destination.exists() and not destination.is_dir():
        raise ValueError("retired experiment registry must be a directory")
    if source.is_file():
        _validate_retired_experiment_marker(source, destination)
        if not destination.is_dir():
            raise ValueError("retired experiment registry destination is missing")
        return {"status": "already_migrated", "destination": str(destination)}
    if not source.exists():
        if not destination.is_dir():
            return {"status": "absent", "destination": None}
        _write_retired_experiment_marker(source, destination)
        return {"status": "already_migrated", "destination": str(destination)}
    if not source.is_dir():
        raise ValueError("legacy experiment registry must be a directory")
    _private_directory(paths.root / "retired-state")
    _private_directory(archive_root)
    with _legacy_experiment_lock(source):
        if destination.exists():
            raise RuntimeError(
                "legacy and retired experiment registries both exist for "
                f"{paths.project_id}"
            )
        os.replace(source, destination)
        _fsync_directory(destination.parent)
        _write_retired_experiment_marker(source, destination)
    return {"status": "archived", "destination": str(destination)}


def _migrate_project_state(paths: ControllerPaths) -> dict[str, Any]:
    pending = output_sync_paths(paths.registry_root).pending_dir
    if pending.is_symlink():
        raise ValueError("output-sync pending directory must not be a symlink")
    has_pending = pending.is_dir() and any(pending.glob("rr-*.json"))
    if has_pending and not paths.config_path.is_file():
        raise FileNotFoundError(
            f"cannot migrate pending output sync without project config: {paths.project_id}"
        )
    if paths.config_path.is_file():
        execution_paths = project_paths(paths.config_path)
        if execution_paths.registry_root != paths.registry_root:
            raise ValueError(
                f"controller project config resolves outside project: {paths.project_id}"
            )
        sync = migrate_legacy_pending_intents(execution_paths)
    else:
        sync = {"scanned": 0, "migrated_run_ids": []}
    return {
        "project_id": paths.project_id,
        "output_sync": sync,
        "retired_experiment_registry": _archive_legacy_experiment_registry(paths),
    }


def activate_release(controller_root: Path, revision: str) -> dict[str, Any]:
    if not REVISION_RE.fullmatch(revision):
        raise ValueError("release revision must be a full lowercase Git SHA")
    if SOURCE_REVISION != revision:
        raise ValueError("release runtime revision does not match requested activation")
    root = controller_root.expanduser().resolve()
    runner_root = root / "runner"
    release = runner_root / "releases" / revision
    receipt = release / ".deployed-revision"
    if not release.is_dir() or release.is_symlink():
        raise FileNotFoundError(f"staged controller release does not exist: {revision}")
    if receipt.read_text(encoding="utf-8").strip() != revision:
        raise ValueError("staged controller release revision receipt does not match")

    project_ids = _project_ids(root)
    with scheduler_lock(root):
        leases = _active_leases(root)
        if leases:
            detail = ", ".join(
                f"{lease.project_id}:{lease.server}:{lease.run_id}"
                for lease in leases
            )
            raise RuntimeError(f"controller release blocked by active dispatch lease: {detail}")
        stopped = _stop_controller_workers(project_ids)
        migrations = [
            _migrate_project_state(controller_paths(root, project_id))
            for project_id in project_ids
        ]
        runner_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = runner_root / "current"
        previous = os.readlink(current) if current.is_symlink() else None
        temporary = runner_root / f".current-{revision}-{os.getpid()}"
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        temporary.symlink_to(Path("releases") / revision)
        os.replace(temporary, current)

    return {
        "revision": revision,
        "previous": previous,
        "stopped_dispatchers": stopped["dispatchers"],
        "stopped_output_sync_workers": stopped["output_sync_workers"],
        "state_migrations": migrations,
        "projects": project_ids,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gate private controller release activation.")
    parser.add_argument("--controller-root", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("inspect")
    activate = subparsers.add_parser("activate")
    activate.add_argument("--revision", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "inspect":
            result = inspect_release_gate(args.controller_root)
        else:
            result = activate_release(args.controller_root, args.revision)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
