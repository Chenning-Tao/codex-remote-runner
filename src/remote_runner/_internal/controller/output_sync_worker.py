from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from ..execution_registry import project_paths
from ..output_sync import (
    has_pending,
    has_unpruned_completed_syncs,
    load_config,
    process_pending_once,
)
from ..tmux import exact_tmux_target, output_sync_tmux_session, resolve_tmux_executable
from .output_prune import prune_outputs
from .registry import ControllerPaths, controller_paths


def ensure_output_sync_worker(
    paths: ControllerPaths,
    *,
    timeout: int,
    interval: int,
) -> bool:
    if not paths.config_path.is_file():
        return False
    execution_paths = project_paths(paths.config_path)
    config = load_config(execution_paths.registry_root)
    if config is None or config.paused:
        return False
    if not has_pending(
        execution_paths.registry_root
    ) and not has_unpruned_completed_syncs(
        execution_paths.registry_root,
        config.prune_source_servers,
    ):
        return False
    tmux = resolve_tmux_executable()
    session = output_sync_tmux_session(paths.project_id)
    target = exact_tmux_target(session)
    exists = subprocess.run(
        [tmux, "has-session", "-t", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode == 0:
        return False
    started = subprocess.run(
        [
            tmux,
            "new-session",
            "-d",
            "-s",
            session,
            sys.executable,
            "-m",
            "remote_runner._internal.controller.output_sync_worker",
            "--controller-root",
            str(paths.root),
            "--project-id",
            paths.project_id,
            "--timeout",
            str(timeout),
            "--interval",
            str(interval),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        raced = subprocess.run(
            [tmux, "has-session", "-t", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if raced.returncode == 0:
            return False
        raise RuntimeError(
            started.stderr.strip() or "failed to start controller output-sync worker"
        )
    return True


def run_worker(
    paths: ControllerPaths,
    *,
    timeout: int,
    interval: int,
    once: bool,
) -> int:
    if not paths.config_path.is_file():
        return 0
    execution_paths = project_paths(paths.config_path)
    while True:
        result = process_pending_once(execution_paths, connect_timeout=timeout)
        config = load_config(execution_paths.registry_root)
        if config is not None and not config.paused and config.prune_source_servers:
            result["prune_after_sync"] = prune_outputs(
                argparse.Namespace(
                    controller_root=paths.root,
                    project_id=paths.project_id,
                    run_id=None,
                    server=list(config.prune_source_servers),
                    apply=True,
                    timeout=timeout,
                )
            )
        else:
            result["prune_after_sync"] = {
                "applied": False,
                "servers": [],
                "candidate_count": 0,
                "pruned_count": 0,
                "failed_count": 0,
            }
        print(json.dumps(result, sort_keys=True), flush=True)
        prune_remaining = (
            config is not None
            and not config.paused
            and has_unpruned_completed_syncs(
                execution_paths.registry_root,
                config.prune_source_servers,
            )
        )
        if (
            once
            or not result.get("enabled")
            or (int(result.get("remaining", 0)) == 0 and not prune_remaining)
        ):
            return 0
        delay = interval if config is None else config.retry_seconds
        time.sleep(delay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize terminal-run outputs.")
    parser.add_argument("--controller-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.interval <= 0:
        parser.error("timeout and interval must be positive")
    try:
        paths = controller_paths(args.controller_root, args.project_id)
        return run_worker(
            paths,
            timeout=args.timeout,
            interval=args.interval,
            once=args.once,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
