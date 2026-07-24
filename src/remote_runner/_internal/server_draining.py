from __future__ import annotations

import argparse
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config


def update(args: argparse.Namespace, *, drained: bool) -> dict[str, Any]:
    if args.server == "all":
        raise ValueError("--server must name one configured server, not 'all'")
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    if args.server not in config.remotes:
        raise ValueError(f"server {args.server!r} is not configured for this project")
    return call_controller(
        config,
        "drain-server" if drained else "resume-server",
        timeout=args.timeout,
        action_args=("--server", args.server),
    )
