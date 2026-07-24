from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..._revision import SOURCE_REVISION
from ..execution_registry import load_yaml
from ..tmux import dispatcher_tmux_session, exact_tmux_target, resolve_tmux_executable
from .registry import (
    controller_scheduler_paths,
    scheduler_lock,
)


REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


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


def _stop_dispatchers(project_ids: list[str]) -> list[str]:
    if not project_ids:
        return []
    tmux = resolve_tmux_executable()
    stopped: list[str] = []
    for project_id in project_ids:
        target = exact_tmux_target(dispatcher_tmux_session(project_id))
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
                or f"failed to stop controller dispatcher for {project_id}"
            )
        stopped.append(project_id)
    return stopped


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
        stopped = _stop_dispatchers(project_ids)
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
        "stopped_dispatchers": stopped,
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
