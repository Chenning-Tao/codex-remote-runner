from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import load_yaml, resolve_project_config
from .machine_identity import normalize_machine_id
from .pool import DEFAULT_SERVER_REGISTRY


def update(args: argparse.Namespace, *, drained: bool) -> dict[str, Any]:
    if args.server == "all":
        raise ValueError("--server must name one configured server, not 'all'")
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    if args.server not in config.remotes:
        raise ValueError(f"server {args.server!r} is not configured for this project")
    registry_path = Path(
        getattr(args, "server_registry", DEFAULT_SERVER_REGISTRY)
    ).expanduser()
    registry = load_yaml(registry_path)
    servers = registry.get("servers")
    if not isinstance(servers, dict) or not isinstance(servers.get(args.server), dict):
        raise ValueError(f"server {args.server!r} is not in the global registry")
    machine_id, _source = normalize_machine_id(
        servers[args.server].get("machine_id"),
        server_name=args.server,
    )
    return call_controller(
        config,
        "drain-server" if drained else "resume-server",
        timeout=args.timeout,
        action_args=("--server", args.server),
        payload={"machine_id": machine_id},
    )


def request_server_drain_update(
    args: argparse.Namespace,
    server: str,
    drained: bool,
) -> dict[str, Any]:
    update_args = argparse.Namespace(**vars(args))
    update_args.server = server
    return update(update_args, drained=drained)
