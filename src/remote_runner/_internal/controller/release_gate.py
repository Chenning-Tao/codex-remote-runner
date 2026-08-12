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
from ..execution_registry import project_paths
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
    load_server_lease,
    scheduler_lock,
)


REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^remote-runner ([^ ]+) \(([0-9a-f]{40})\)$")
RETIRED_EXPERIMENT_MARKER_SCHEMA = 1


@dataclass(frozen=True)
class ActiveLease:
    project_id: str
    server: str
    machine_id: str
    run_id: str
    expires_at: float
    heartbeat_expired: bool


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
            lease = load_server_lease(lease_path)
            expires_at = float(lease["expires_at"])
            durable_dispatch = lease["kind"] == "dispatch"
            if expires_at <= timestamp and not durable_dispatch:
                continue
            active.append(
                ActiveLease(
                    project_id=str(lease["project_id"]),
                    server=str(lease["server"]),
                    machine_id=str(lease["machine_id"]),
                    run_id=str(lease["run_id"]),
                    expires_at=expires_at,
                    heartbeat_expired=expires_at <= timestamp,
                )
            )

    return active


def _executable_receipt(path: Path) -> dict[str, Any]:
    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(path), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env=clean_environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip() or f"version probe failed for {path}"
        )
    version = completed.stdout.strip()
    match = VERSION_RE.fullmatch(version)
    if match is None:
        raise RuntimeError(f"invalid remote-runner version receipt from {path}")
    return {
        "path": str(path),
        "version": match.group(1),
        "revision": match.group(2),
        "raw": version,
    }


def _optional_executable_receipt(path: Path | None) -> dict[str, Any] | None:
    if path is None or (not path.exists() and not path.is_symlink()):
        return None
    return _executable_receipt(path)


def inspect_release_gate(
    controller_root: Path,
    *,
    now: float | None = None,
    global_cli: Path | None = None,
) -> dict[str, Any]:
    root = controller_root.expanduser().resolve()
    project_ids = _project_ids(root)
    leases = _active_leases(root, now=now)
    current_cli = root / "runner" / "current" / "venv" / "bin" / "remote-runner"
    return {
        "revision": SOURCE_REVISION,
        "projects": project_ids,
        "active_leases": [lease.__dict__ for lease in leases],
        "controller_global_cli": _optional_executable_receipt(global_cli),
        "controller_private_current": _optional_executable_receipt(current_cli),
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


def _activate_runtime_links(
    *,
    runner_root: Path,
    revision: str,
    global_cli: Path | None,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any]]:
    runner_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = runner_root / "current"
    if current.exists() and not current.is_symlink():
        raise ValueError("controller private current must be a symlink")
    previous = os.readlink(current) if current.is_symlink() else None
    current_temporary = runner_root / f".current-{revision}-{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        current_temporary.unlink()
    current_temporary.symlink_to(Path("releases") / revision)

    global_backup: Path | None = None
    global_temporary: Path | None = None
    if global_cli is not None:
        if not global_cli.is_absolute() or global_cli.name != "remote-runner":
            raise ValueError("controller global CLI path must be absolute remote-runner")
        if not global_cli.parent.is_dir() or global_cli.parent.is_symlink():
            raise ValueError("controller global CLI parent must be a real directory")
        global_temporary = global_cli.parent / f".remote-runner-{revision}-{os.getpid()}"
        global_backup = global_cli.parent / f".remote-runner-backup-{os.getpid()}"
        for temporary in (global_temporary, global_backup):
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        global_temporary.symlink_to(current / "venv" / "bin" / "remote-runner")

    moved_global = False
    installed_global = False
    switched_current = False
    try:
        if global_cli is not None and (global_cli.exists() or global_cli.is_symlink()):
            assert global_backup is not None
            os.replace(global_cli, global_backup)
            moved_global = True
        if global_cli is not None:
            assert global_temporary is not None
            os.replace(global_temporary, global_cli)
            installed_global = True
        os.replace(current_temporary, current)
        switched_current = True
        private_path = current / "venv" / "bin" / "remote-runner"
        private_receipt = (
            _executable_receipt(private_path)
            if global_cli is not None
            else {"path": str(private_path), "revision": revision, "raw": None}
        )
        if private_receipt["revision"] != revision:
            raise RuntimeError("controller private current revision does not match activation")
        global_receipt = _optional_executable_receipt(global_cli)
        if global_cli is not None and (
            global_receipt is None or global_receipt["revision"] != revision
        ):
            raise RuntimeError("controller global CLI revision does not match activation")
    except BaseException:
        if switched_current:
            rollback = runner_root / f".rollback-current-{os.getpid()}"
            with contextlib.suppress(FileNotFoundError):
                rollback.unlink()
            if previous is None:
                with contextlib.suppress(FileNotFoundError):
                    current.unlink()
            else:
                rollback.symlink_to(previous)
                os.replace(rollback, current)
        if global_cli is not None and installed_global:
            with contextlib.suppress(FileNotFoundError):
                global_cli.unlink()
        if global_cli is not None and moved_global:
            assert global_backup is not None
            os.replace(global_backup, global_cli)
        raise
    finally:
        with contextlib.suppress(FileNotFoundError):
            current_temporary.unlink()
        if global_temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                global_temporary.unlink()
    if global_backup is not None:
        with contextlib.suppress(FileNotFoundError):
            global_backup.unlink()
    return previous, global_receipt, private_receipt


def activate_release(
    controller_root: Path,
    revision: str,
    *,
    global_cli: Path | None = None,
) -> dict[str, Any]:
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
                f"{lease.project_id}:{lease.server}[{lease.machine_id}]:{lease.run_id}"
                for lease in leases
            )
            raise RuntimeError(f"controller release blocked by active dispatch lease: {detail}")
        stopped = _stop_controller_workers(project_ids)
        migrations = [
            _migrate_project_state(controller_paths(root, project_id))
            for project_id in project_ids
        ]
        previous, global_receipt, private_receipt = _activate_runtime_links(
            runner_root=runner_root,
            revision=revision,
            global_cli=global_cli,
        )

    return {
        "revision": revision,
        "previous": previous,
        "stopped_dispatchers": stopped["dispatchers"],
        "stopped_output_sync_workers": stopped["output_sync_workers"],
        "state_migrations": migrations,
        "projects": project_ids,
        "controller_global_cli": global_receipt,
        "controller_private_current": private_receipt,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gate private controller release activation.")
    parser.add_argument("--controller-root", type=Path, required=True)
    parser.add_argument("--global-cli", type=Path)
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
            result = inspect_release_gate(
                args.controller_root,
                global_cli=args.global_cli,
            )
        else:
            result = activate_release(
                args.controller_root,
                args.revision,
                global_cli=args.global_cli,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
