from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .config import ManagedProjectConfig, load_managed_project_config
from .controller.client import call_controller
from .execution_registry import load_yaml, resolve_project_config
from .machine_identity import normalize_machine_id
from .pool import DEFAULT_SERVER_REGISTRY, resolve_ssh_targets


DASHBOARD_SCHEMA = 1


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _test_slots(server: dict[str, Any], name: str) -> int:
    testing = server.get("testing")
    if testing is None:
        return 0
    if not isinstance(testing, dict):
        raise ValueError(f"configured testing for {name!r} must be a mapping")
    return _positive_int(testing.get("slots"), f"configured testing slots for {name!r}")


def build_server_inventory(
    config: ManagedProjectConfig,
    server_registry_path: Path,
) -> list[dict[str, Any]]:
    registry = load_yaml(server_registry_path.expanduser().resolve(strict=True))
    servers = registry.get("servers")
    if not isinstance(servers, dict):
        raise ValueError("global server registry must contain a 'servers' mapping")

    inventory: list[dict[str, Any]] = []
    for name, runtime in sorted(config.remotes.items()):
        raw = servers.get(name)
        base: dict[str, Any] = {
            "name": name,
            "machine_id": name,
            "machine_id_source": "legacy-name",
            "machine_fingerprint": None,
            "enabled": runtime.enabled,
            "auto_select": runtime.auto_select,
            "python": runtime.python,
            "configured_cores": None,
            "configured_memory_gb": None,
            "standard_slots": 1,
            "test_slots": 0,
            "testing_enabled": name in config.scheduling.testing_servers,
            "output_root_configured": runtime.output_root is not None,
            "endpoints": [],
        }
        if raw is None:
            base.update(
                enabled=False,
                configuration_error="server is not in the global registry",
            )
            inventory.append(base)
            continue
        if not isinstance(raw, dict):
            base.update(
                enabled=False,
                configuration_error="global server entry must be a mapping",
            )
            inventory.append(base)
            continue
        try:
            globally_enabled = raw.get("enabled", True)
            if not isinstance(globally_enabled, bool):
                raise ValueError(
                    f"configured enabled flag for {name!r} must be boolean"
                )
            cores = _positive_int(raw.get("cores"), f"configured cores for {name!r}")
            memory_gb = raw.get("memory_gb")
            if memory_gb is not None:
                memory_gb = _positive_int(
                    memory_gb, f"configured memory_gb for {name!r}"
                )
            slots = _test_slots(raw, name)
            machine_id, machine_id_source = normalize_machine_id(
                raw.get("machine_id"),
                server_name=name,
            )
            endpoints = [
                {"ssh": ssh, "ssh_profile": profile}
                for ssh, profile in resolve_ssh_targets(raw, name, "auto")
            ]
            if not endpoints:
                raise ValueError(f"server {name!r} has no SSH endpoint")
        except ValueError as exc:
            base.update(enabled=False, configuration_error=str(exc))
            inventory.append(base)
            continue
        base.update(
            enabled=runtime.enabled and globally_enabled,
            configured_cores=cores,
            configured_memory_gb=memory_gb,
            machine_id=machine_id,
            machine_id_source=machine_id_source,
            machine_fingerprint=None,
            test_slots=slots,
            endpoints=endpoints,
        )
        inventory.append(base)
    return inventory


def query_dashboard(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    registry_path = getattr(args, "server_registry", DEFAULT_SERVER_REGISTRY)
    inventory = build_server_inventory(config, registry_path)
    return call_controller(
        config,
        "dashboard",
        timeout=args.timeout,
        payload={"schema_version": DASHBOARD_SCHEMA, "servers": inventory},
    )
