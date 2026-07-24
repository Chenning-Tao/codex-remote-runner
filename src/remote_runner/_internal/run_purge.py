from __future__ import annotations

import argparse

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config


def request_run_purge(args: argparse.Namespace) -> dict[str, object]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    action_args = ["--run-id", args.run_id, "--reason", args.reason]
    if args.replacement_run_id is not None:
        action_args.extend(["--replacement-run-id", args.replacement_run_id])
    if args.no_replacement:
        action_args.append("--no-replacement")
    if args.apply:
        action_args.append("--apply")
    return call_controller(
        config,
        "purge-run",
        timeout=args.timeout,
        action_args=tuple(action_args),
        overall_timeout=max(3600, args.timeout + 300),
    )
