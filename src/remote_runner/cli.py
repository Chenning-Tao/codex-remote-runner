from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from ._internal import (
    cleanup,
    monitoring,
    output_prune,
    output_sync_client,
    pool_sync,
    preparation,
    run_purge,
    server_addition,
    server_draining,
    stopping,
    submission,
    task_purge,
    waiting,
    wakeup,
    wakeup_worker,
)
from ._internal.pool import DEFAULT_SERVER_REGISTRY
from ._internal.preparation_manifest import write_preparation_manifest
from ._internal.result_metadata import MONITOR_RESULT_INTENTS, RESULT_INTENTS
from ._internal.scheduling import QUEUE_PRIORITIES, WORKLOAD_CLASSES
from ._revision import SOURCE_REVISION


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return parsed


def _add_project_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-config", type=Path)


def _add_source_preparation(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-repo",
        type=Path,
        help="absolute clean local Git repository to submit instead of source.local_repo",
    )
    parser.add_argument(
        "--server-registry",
        type=Path,
        default=DEFAULT_SERVER_REGISTRY,
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--server",
        metavar="NAME|all",
        help=(
            "require exactly one server, or use 'all' for the current automatic "
            "pool; explicit preparation fails if it is unavailable"
        ),
    )
    selection.add_argument(
        "--candidate-server",
        dest="candidate_servers",
        action="append",
        metavar="NAME",
        help=("limit placement to an allowed server; repeat for a candidate pool"),
    )
    parser.add_argument("--ssh-profile", default="auto")
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--prepare-timeout", type=int, default=60)


def _add_wait_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--until",
        choices=("execution-terminal",),
        default="execution-terminal",
    )
    parser.add_argument(
        "--max-wait",
        type=_positive_int,
        help="stop waiting after this many seconds without stopping the run",
    )
    parser.add_argument(
        "--connection-grace",
        type=_positive_int,
        default=300,
        help="retry controller transport failures for this many seconds",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remote-runner",
        description="Manage durable workloads on a project-configured remote server pool.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__} ({SOURCE_REVISION})",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    prepare_parser = subparsers.add_parser(
        "prepare",
        help="prepare one clean revision for reusable submissions",
    )
    _add_project_config(prepare_parser)
    _add_source_preparation(prepare_parser)
    prepare_parser.add_argument("--out", type=Path, required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="prepare or reuse a revision and submit a durable run",
    )
    _add_project_config(run_parser)
    _add_source_preparation(run_parser)
    run_parser.add_argument(
        "--prepared-manifest",
        type=Path,
        help="reuse a validated preparation manifest for the current clean revision",
    )
    run_parser.add_argument(
        "--min-cores",
        dest="minimum_cores",
        type=int,
        default=1,
        help=(
            "require candidates to have at least this many configured CPU cores; "
            "cannot be combined with --candidate-server"
        ),
    )
    run_parser.add_argument("--label", required=True)
    run_parser.add_argument("--task-id", required=True)
    run_parser.add_argument(
        "--result-intent",
        choices=RESULT_INTENTS,
        required=True,
        help="declare whether this run is a result candidate, supporting evidence, or excluded",
    )
    run_parser.add_argument(
        "--tag",
        dest="result_tags",
        action="append",
        metavar="KEY=VALUE",
        help="attach open result metadata; repeat for multiple tags",
    )
    run_parser.add_argument(
        "--workload-class",
        choices=WORKLOAD_CLASSES,
        default="standard",
        help="submit standard exclusive work or a configured test-lane workload",
    )
    run_parser.add_argument(
        "--queue-priority",
        choices=QUEUE_PRIORITIES,
        default="normal",
        help="place urgent work ahead of normal queued work; FIFO within each priority",
    )
    run_parser.add_argument("--command", required=True)
    run_parser.add_argument(
        "--output-relpath",
        help="portable relative POSIX path resolved against the selected output_root",
    )
    run_parser.add_argument("--output-metadata")
    run_parser.add_argument("--privacy", choices=("process-title",))
    run_parser.add_argument("--run-id")
    run_parser.add_argument(
        "--wait",
        action="store_true",
        help="wait for the submitted run to reach an authoritative terminal state",
    )
    _add_wait_options(run_parser)

    monitor_parser = subparsers.add_parser(
        "monitor",
        help="query controller queue and execution state",
    )
    _add_project_config(monitor_parser)
    selector = monitor_parser.add_mutually_exclusive_group()
    selector.add_argument("--run-id")
    selector.add_argument("--task-id")
    monitor_parser.add_argument(
        "--result-intent",
        choices=MONITOR_RESULT_INTENTS,
        help="filter queue and execution records by declared result intent",
    )
    monitor_parser.add_argument("--timeout", type=int, default=8)

    wait_parser = subparsers.add_parser(
        "wait",
        help="wait for one run to reach an authoritative terminal state",
    )
    _add_project_config(wait_parser)
    wait_parser.add_argument("--run-id", required=True)
    wait_parser.add_argument("--timeout", type=int, default=8)
    _add_wait_options(wait_parser)

    wakeup_parser = subparsers.add_parser(
        "wakeup",
        help="wake a Codex task once a run cohort finishes or needs attention",
    )
    wakeup_actions = wakeup_parser.add_subparsers(
        dest="wakeup_action",
        required=True,
        metavar="{register,list,install,uninstall,cancel}",
    )
    wakeup_register = wakeup_actions.add_parser(
        "register",
        help="register a durable wakeup for one exact run cohort",
    )
    _add_project_config(wakeup_register)
    wakeup_register.add_argument(
        "--run-id",
        dest="run_ids",
        action="append",
        required=True,
        help="exact run in the cohort; repeat for multiple runs",
    )
    wakeup_register.add_argument(
        "--codex-thread-id",
        help="target Codex task; defaults to CODEX_THREAD_ID",
    )
    wakeup_register.add_argument(
        "--codex-executable",
        type=Path,
        help="absolute Codex CLI executable; defaults to PATH discovery",
    )
    wakeup_register.add_argument("--timeout", type=int, default=8)
    wakeup_actions.add_parser(
        "list",
        help="list pending and ready wakeup subscriptions",
    )
    wakeup_actions.add_parser(
        "install",
        help="install on-demand macOS restart recovery for pending wakeups",
    )
    wakeup_actions.add_parser(
        "uninstall",
        help="remove the macOS wakeup restart-recovery supervisor",
    )
    wakeup_cancel = wakeup_actions.add_parser(
        "cancel",
        help="cancel one pending wakeup subscription",
    )
    wakeup_cancel.add_argument("--wake-id", required=True)
    wakeup_worker_parser = wakeup_actions.add_parser("worker")
    wakeup_worker_parser.add_argument("--state-root", type=Path, required=True)
    wakeup_worker_parser.add_argument("--once", action="store_true")
    wakeup_worker_parser.add_argument("--supervised", action="store_true")

    tui_parser = subparsers.add_parser(
        "tui",
        help="open the optional interactive human dashboard",
    )
    _add_project_config(tui_parser)
    tui_parser.add_argument(
        "--server-registry",
        type=Path,
        default=DEFAULT_SERVER_REGISTRY,
    )
    tui_parser.add_argument("--timeout", type=int, default=8)
    tui_parser.add_argument(
        "--stop-timeout",
        type=_positive_int,
        default=10,
        help="wait this many seconds before escalating a selected workload stop",
    )

    web_parser = subparsers.add_parser(
        "web",
        help="open the optional read-only dashboard in a local browser",
    )
    _add_project_config(web_parser)
    web_parser.add_argument(
        "--server-registry",
        type=Path,
        default=DEFAULT_SERVER_REGISTRY,
    )
    web_parser.add_argument("--timeout", type=int, default=8)
    web_parser.add_argument(
        "--stop-timeout",
        type=_positive_int,
        default=10,
        help="wait this many seconds before escalating a web workload stop",
    )
    web_parser.add_argument("--port", type=_port, default=8765)
    web_parser.add_argument(
        "--no-open",
        action="store_true",
        help="start the local dashboard without opening a browser",
    )

    stop_parser = subparsers.add_parser(
        "stop",
        help="stop one queued or running workload",
    )
    _add_project_config(stop_parser)
    stop_parser.add_argument("--run-id", required=True)
    stop_parser.add_argument("--timeout", type=int, default=10)

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="review or purge authoritative stopped records",
    )
    _add_project_config(cleanup_parser)
    cleanup_parser.add_argument("--run-id")
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="perform cleanup; omission produces a dry-run candidate list",
    )
    cleanup_parser.add_argument("--timeout", type=int, default=10)

    run_purge_parser = subparsers.add_parser(
        "purge-run",
        help="remove one failed run and its exclusively owned artifacts",
    )
    _add_project_config(run_purge_parser)
    run_purge_parser.add_argument("--run-id", required=True)
    replacement = run_purge_parser.add_mutually_exclusive_group(required=True)
    replacement.add_argument("--replacement-run-id")
    replacement.add_argument("--no-replacement", action="store_true")
    run_purge_parser.add_argument(
        "--reason",
        default="user confirmed this failed run is no longer needed",
    )
    run_purge_parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the purge; omission produces a dry-run inventory",
    )
    run_purge_parser.add_argument("--timeout", type=int, default=10)

    purge_parser = subparsers.add_parser(
        "purge-task",
        help="remove one exact task and its exclusively owned artifacts",
    )
    _add_project_config(purge_parser)
    purge_parser.add_argument("--task-id", required=True)
    purge_parser.add_argument(
        "--reason",
        default="user confirmed this task is no longer needed",
    )
    purge_parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the purge; omission produces a dry-run inventory",
    )
    purge_parser.add_argument("--timeout", type=int, default=10)

    sync_parser = subparsers.add_parser(
        "sync-outputs",
        help="configure and start automatic synchronization of succeeded outputs",
    )
    _add_project_config(sync_parser)
    sync_parser.add_argument("--timeout", type=int, default=10)

    prune_outputs_parser = subparsers.add_parser(
        "prune-outputs",
        help="remove verified synchronized source outputs from compute servers",
    )
    _add_project_config(prune_outputs_parser)
    prune_outputs_parser.add_argument("--run-id")
    prune_outputs_parser.add_argument(
        "--apply",
        action="store_true",
        help="perform deletion; omission produces a dry-run candidate list",
    )
    prune_outputs_parser.add_argument("--timeout", type=int, default=10)

    pool_sync_parser = subparsers.add_parser(
        "sync-pool",
        help="prepare new automatic servers for queued all-server jobs",
    )
    _add_project_config(pool_sync_parser)
    pool_sync_parser.add_argument("--source-repo", type=Path)
    pool_sync_parser.add_argument(
        "--server-registry",
        type=Path,
        default=DEFAULT_SERVER_REGISTRY,
    )
    pool_sync_parser.add_argument("--ssh-profile", default="auto")
    pool_sync_parser.add_argument("--timeout", type=int, default=8)
    pool_sync_parser.add_argument("--prepare-timeout", type=int, default=60)

    add_server_parser = subparsers.add_parser(
        "add-server",
        help="allow one additional server for a queued run",
    )
    _add_project_config(add_server_parser)
    add_server_parser.add_argument("--source-repo", type=Path)
    add_server_parser.add_argument(
        "--server-registry",
        type=Path,
        default=DEFAULT_SERVER_REGISTRY,
    )
    add_server_parser.add_argument("--run-id", required=True)
    add_server_parser.add_argument("--server", required=True)
    add_server_parser.add_argument("--ssh-profile", default="auto")
    add_server_parser.add_argument("--timeout", type=int, default=8)
    add_server_parser.add_argument("--prepare-timeout", type=int, default=60)
    for command, help_text in (
        ("drain-server", "prevent new dispatches to one controller-wide server"),
        ("resume-server", "allow dispatches to a previously drained server"),
    ):
        server_state_parser = subparsers.add_parser(command, help=help_text)
        _add_project_config(server_state_parser)
        server_state_parser.add_argument("--server", required=True)
        server_state_parser.add_argument("--timeout", type=int, default=8)
    return parser


def _execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.subcommand == "prepare":
        result = preparation.prepare(args)
        write_preparation_manifest(args.out, result)
        return result, 0
    if args.subcommand == "run":
        if not args.wait and args.max_wait is not None:
            raise ValueError("--max-wait requires --wait")
        result = submission.submit(args)
        if args.wait:
            print(
                f"[remote-runner run] submitted run_id={result['run_id']}; waiting",
                file=sys.stderr,
                flush=True,
            )
            waited = waiting.wait_for_run(
                argparse.Namespace(
                    project_config=args.project_config,
                    run_id=result["run_id"],
                    timeout=args.timeout,
                    until=args.until,
                    max_wait=args.max_wait,
                    connection_grace=args.connection_grace,
                )
            )
            return {**result, "wait": waited}, waiting.wait_exit_code(waited)
        return result, 0
    if args.subcommand == "monitor":
        return monitoring.query_controller(args), 0
    if args.subcommand == "wait":
        result = waiting.wait_for_run(args)
        return result, waiting.wait_exit_code(result)
    if args.subcommand == "wakeup":
        if args.wakeup_action == "register":
            return wakeup.register(args), 0
        if args.wakeup_action == "list":
            return wakeup.list_registered(args), 0
        if args.wakeup_action == "install":
            return wakeup.install_supervisor(args), 0
        if args.wakeup_action == "uninstall":
            return wakeup.uninstall_supervisor(args), 0
        if args.wakeup_action == "cancel":
            return wakeup.cancel(args), 0
        if args.wakeup_action == "worker":
            return (
                wakeup_worker.run_worker(
                    wakeup.wakeup_paths(args.state_root),
                    once=args.once,
                    supervised=args.supervised,
                ),
                0,
            )
        raise AssertionError(f"unhandled wakeup action: {args.wakeup_action}")
    if args.subcommand == "stop":
        return stopping.request_stop(args), 0
    if args.subcommand == "cleanup":
        result = cleanup.request_cleanup(args)
        failed = args.apply and int(result.get("failed_count", 0)) > 0
        return result, int(failed)
    if args.subcommand == "purge-run":
        result = run_purge.request_run_purge(args)
        incomplete = args.apply and result.get("status") not in {
            "complete",
            "already_purged",
        }
        return result, int(incomplete)
    if args.subcommand == "purge-task":
        result = task_purge.request_task_purge(args)
        incomplete = args.apply and result.get("status") not in {
            "complete",
            "already_purged",
        }
        return result, int(incomplete)
    if args.subcommand == "sync-outputs":
        return output_sync_client.configure(args), 0
    if args.subcommand == "prune-outputs":
        result = output_prune.request_output_prune(args)
        failed = args.apply and int(result.get("failed_count", 0)) > 0
        return result, int(failed)
    if args.subcommand == "sync-pool":
        return pool_sync.sync(args), 0
    if args.subcommand == "add-server":
        return server_addition.add(args), 0
    if args.subcommand == "drain-server":
        return server_draining.update(args, drained=True), 0
    if args.subcommand == "resume-server":
        return server_draining.update(args, drained=False), 0
    raise AssertionError(f"unhandled subcommand: {args.subcommand}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.subcommand == "tui":
        try:
            from .tui import run_tui
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if any(
                missing == dependency or missing.startswith(f"{dependency}.")
                for dependency in ("rich", "textual")
            ):
                parser.error(
                    "the TUI optional dependency is not installed; "
                    "install codex-remote-runner with the 'tui' extra"
                )
            raise
        try:
            run_tui(args)
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        return 0
    if args.subcommand == "web":
        try:
            from .web_app import run_web
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if any(
                missing == dependency or missing.startswith(f"{dependency}.")
                for dependency in ("starlette", "uvicorn")
            ):
                parser.error(
                    "the web dashboard optional dependency is not installed; "
                    "install codex-remote-runner with the 'web' extra"
                )
            raise
        try:
            run_web(args)
        except (OSError, RuntimeError, ValueError) as exc:
            parser.error(str(exc))
        return 0
    try:
        result, returncode = _execute(args)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
