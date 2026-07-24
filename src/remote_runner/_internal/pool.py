from __future__ import annotations

import shlex
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import ManagedProjectConfig
from .execution_registry import load_yaml
from .scheduling import normalize_minimum_cores


DEFAULT_SERVER_REGISTRY = Path("~/.codex/remote-servers.yaml").expanduser()
DEFAULT_SSH_PROFILE = "auto"
AUTO_SSH_PROFILES = {"auto", "default"}
ALL_SERVERS = "all"


def normalize_explicit_server(value: object) -> str | None:
    if value is None or value == ALL_SERVERS:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("--server must be a non-empty server name or 'all'")
    return value


def normalize_candidate_servers(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError("--candidate-server must be repeated with a server name")
    candidates = tuple(value)
    if any(not isinstance(name, str) or not name.strip() for name in candidates):
        raise ValueError("--candidate-server must be a non-empty server name")
    if len(set(candidates)) != len(candidates):
        raise ValueError("--candidate-server must not contain duplicates")
    return candidates


def probe_endpoint(ssh: str, timeout: int) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("endpoint probe timeout must be positive")
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={timeout}",
        ssh,
        f"sh -c {shlex.quote('true')}",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired as exc:
        return {"reachable": False, "error": f"probe timed out after {exc.timeout}s"}
    if completed.returncode != 0:
        return {
            "reachable": False,
            "error": completed.stderr.strip() or f"ssh exited with {completed.returncode}",
        }
    return {"reachable": True}


def resolve_ssh_targets(
    server: dict[str, Any],
    name: str,
    ssh_profile: str,
) -> list[tuple[str, str]]:
    endpoints = server.get("endpoints", {})
    fallback = str(server.get("ssh", name))
    targets: list[tuple[str, str]] = []

    if isinstance(endpoints, dict) and ssh_profile not in AUTO_SSH_PROFILES:
        if ssh_profile in endpoints:
            targets.append((str(endpoints[ssh_profile]), ssh_profile))
        if fallback not in [target for target, _profile in targets]:
            targets.append((fallback, "ssh"))
        return targets

    endpoint_order = server.get("endpoint_order", [])
    if isinstance(endpoints, dict):
        if not isinstance(endpoint_order, list) or not endpoint_order:
            endpoint_order = ["intranet", "tailscale", "default"]
        for profile_name in endpoint_order:
            if profile_name in endpoints:
                targets.append((str(endpoints[profile_name]), str(profile_name)))

    if fallback not in [target for target, _profile in targets]:
        targets.append((fallback, "ssh"))
    return targets


def _configured_candidates(
    server_registry: dict[str, Any],
    project_config: ManagedProjectConfig,
    explicit_server: str | None,
    candidate_servers: tuple[str, ...] | None,
    minimum_cores: int,
) -> list[dict[str, Any]]:
    minimum_cores = normalize_minimum_cores(minimum_cores)
    servers = server_registry.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("global server registry must contain a 'servers' mapping")
    project_names = project_config.candidate_names(explicit_server, candidate_servers)
    if explicit_server is not None and explicit_server not in servers:
        raise ValueError(f"server {explicit_server!r} is not in the global registry")

    candidates: list[dict[str, Any]] = []
    for name in project_names:
        if name not in servers:
            continue
        server = servers[name]
        if not isinstance(server, dict):
            raise ValueError(f"global server entry {name!r} must be a mapping")
        if not server.get("enabled", True):
            if name == explicit_server:
                raise ValueError(f"server {name!r} is disabled")
            continue
        cores = server.get("cores")
        if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
            raise ValueError(f"configured cores for {name!r} must be a positive integer")
        if cores < minimum_cores:
            if name == explicit_server:
                raise ValueError(
                    f"server {name!r} has {cores} configured cores, "
                    f"below required minimum {minimum_cores}"
                )
            continue
        priority = server.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError(f"configured priority for {name!r} must be an integer")
        testing = server.get("testing")
        test_slots = 0
        if testing is not None:
            if not isinstance(testing, dict):
                raise ValueError(f"configured testing for {name!r} must be a mapping")
            test_slots = testing.get("slots")
            if (
                isinstance(test_slots, bool)
                or not isinstance(test_slots, int)
                or test_slots <= 0
            ):
                raise ValueError(
                    f"configured testing slots for {name!r} must be a positive integer"
                )
        candidates.append(
            {
                "name": name,
                "server": server,
                "runtime": asdict(project_config.remotes[name]),
                "cores": cores,
                "priority": priority,
                "test_slots": test_slots,
            }
        )
    if not candidates:
        if minimum_cores == 1:
            raise ValueError(
                "no candidate servers; add matching enabled entries to the "
                "global and project configs"
            )
        raise ValueError(
            f"no candidate server has at least {minimum_cores} configured cores"
        )
    return candidates


def probe_project_pool(
    project_config: ManagedProjectConfig,
    server_registry_path: Path,
    *,
    explicit_server: str | None,
    ssh_profile: str,
    timeout: int,
    minimum_cores: int = 1,
    candidate_servers: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    registry = load_yaml(server_registry_path.expanduser())
    candidates = _configured_candidates(
        registry,
        project_config,
        explicit_server,
        candidate_servers,
        minimum_cores,
    )
    for item in candidates:
        attempts: list[dict[str, Any]] = []
        selected_ssh: str | None = None
        selected_profile: str | None = None
        last_probe: dict[str, Any] = {"reachable": False, "error": "no endpoint attempted"}
        for ssh, profile in resolve_ssh_targets(item["server"], item["name"], ssh_profile):
            last_probe = probe_endpoint(ssh, timeout)
            attempts.append({"ssh": ssh, "ssh_profile": profile, "probe": last_probe})
            if last_probe.get("reachable") is True:
                selected_ssh = ssh
                selected_profile = profile
                break
        item["ssh"] = selected_ssh or attempts[-1]["ssh"]
        item["ssh_profile"] = selected_profile or attempts[-1]["ssh_profile"]
        item["requested_ssh_profile"] = ssh_profile
        item["endpoint_attempts"] = attempts
        item["probe"] = last_probe
    return candidates
