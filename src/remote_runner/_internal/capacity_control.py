from __future__ import annotations

import argparse
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config


def request_capacity_update(
    args: argparse.Namespace,
    server: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    if server not in config.remotes:
        raise ValueError(f"server is not configured for this project: {server}")
    return call_controller(
        config,
        "update-server-capacity",
        timeout=args.timeout,
        action_args=("--server", server),
        payload=payload,
    )
