from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import load_yaml, resolve_project_config
from .machine_identity import normalize_machine_id
from .pool import DEFAULT_SERVER_REGISTRY


def request_capacity_update(
    args: argparse.Namespace,
    server: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    if server not in config.remotes:
        raise ValueError(f"server is not configured for this project: {server}")
    registry_path = Path(
        getattr(args, "server_registry", DEFAULT_SERVER_REGISTRY)
    ).expanduser()
    registry = load_yaml(registry_path)
    servers = registry.get("servers")
    if not isinstance(servers, dict) or not isinstance(servers.get(server), dict):
        raise ValueError(f"server is not in the global registry: {server}")
    machine_id, _source = normalize_machine_id(
        servers[server].get("machine_id"),
        server_name=server,
    )
    return call_controller(
        config,
        "update-server-capacity",
        timeout=args.timeout,
        action_args=("--server", server),
        payload={**payload, "machine_id": machine_id},
    )
