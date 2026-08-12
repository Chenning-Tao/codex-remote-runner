from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..dashboard import DASHBOARD_SCHEMA
from ..machine_identity import normalize_server_identity
from .dispatcher import probe_server_state


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"dashboard server {field} must be a non-empty line")
    return value


def _optional_non_negative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"dashboard server {field} must be a non-negative integer")
    return value


def validate_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("schema_version") != DASHBOARD_SCHEMA:
        raise ValueError("unsupported dashboard request schema")
    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, list) or not raw_servers:
        raise ValueError("dashboard request requires servers")

    servers: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw in raw_servers:
        if not isinstance(raw, dict):
            raise ValueError("dashboard server must be a mapping")
        name = _text(raw.get("name"), "name")
        if name in names:
            raise ValueError(f"duplicate dashboard server: {name}")
        names.add(name)
        enabled = raw.get("enabled")
        auto_select = raw.get("auto_select")
        testing_enabled = raw.get("testing_enabled")
        output_root_configured = raw.get("output_root_configured")
        if (
            not isinstance(enabled, bool)
            or not isinstance(auto_select, bool)
            or not isinstance(testing_enabled, bool)
            or not isinstance(output_root_configured, bool)
        ):
            raise ValueError("dashboard server enablement fields must be boolean")
        endpoints_raw = raw.get("endpoints")
        if not isinstance(endpoints_raw, list):
            raise ValueError("dashboard server endpoints must be a list")
        endpoints = []
        for endpoint in endpoints_raw:
            if not isinstance(endpoint, dict):
                raise ValueError("dashboard endpoint must be a mapping")
            endpoints.append(
                {
                    "ssh": _text(endpoint.get("ssh"), "endpoint ssh"),
                    "ssh_profile": _text(
                        endpoint.get("ssh_profile"), "endpoint ssh_profile"
                    ),
                }
            )
        configuration_error = raw.get("configuration_error")
        if configuration_error is not None:
            configuration_error = _text(configuration_error, "configuration_error")
        servers.append(
            normalize_server_identity(
                {
                "name": name,
                "machine_id": raw.get("machine_id"),
                "machine_id_source": raw.get("machine_id_source", "legacy-name"),
                "machine_fingerprint": raw.get("machine_fingerprint"),
                "enabled": enabled,
                "auto_select": auto_select,
                "testing_enabled": testing_enabled,
                "output_root_configured": output_root_configured,
                "python": _text(raw.get("python"), "python"),
                "configured_cores": _optional_non_negative_int(
                    raw.get("configured_cores"), "configured_cores"
                ),
                "configured_memory_gb": _optional_non_negative_int(
                    raw.get("configured_memory_gb"), "configured_memory_gb"
                ),
                "standard_slots": _optional_non_negative_int(
                    raw.get("standard_slots", 1), "standard_slots"
                )
                or 0,
                "test_slots": _optional_non_negative_int(
                    raw.get("test_slots"), "test_slots"
                )
                or 0,
                "endpoints": endpoints,
                "configuration_error": configuration_error,
                }
            )
        )
    return servers


def _probe_server(server: dict[str, Any], timeout: int) -> dict[str, Any]:
    public = {
        field: server[field]
        for field in (
            "name",
            "machine_id",
            "enabled",
            "auto_select",
            "testing_enabled",
            "output_root_configured",
            "configured_cores",
            "configured_memory_gb",
            "standard_slots",
            "test_slots",
            "capacity_revision",
            "capacity_customized",
        )
        if field in server
    }
    configuration_error = server.get("configuration_error")
    if configuration_error is not None:
        return {
            **public,
            "state": "configuration_error",
            "reachable": False,
            "error": configuration_error,
            "active_runs": [],
        }
    if not server["enabled"]:
        return {
            **public,
            "state": "disabled",
            "reachable": None,
            "active_runs": [],
        }

    failures: list[str] = []
    for endpoint in server["endpoints"]:
        try:
            probe = probe_server_state(
                endpoint["ssh"],
                server["python"],
                timeout,
            )
        except (OSError, RuntimeError) as exc:
            failures.append(f"{endpoint['ssh_profile']}: {exc}")
            continue
        active = list(probe.get("active_runs", ()))
        return {
            **public,
            **probe,
            "state": "busy" if active else "idle",
            "ssh_profile": endpoint["ssh_profile"],
            "active_runs": active,
        }
    return {
        **public,
        "state": "unreachable",
        "reachable": False,
        "error": "; ".join(failures) or "no SSH endpoint available",
        "active_runs": [],
    }


def collect_server_snapshot(
    servers: list[dict[str, Any]],
    *,
    timeout: int,
) -> list[dict[str, Any]]:
    workers = min(8, max(1, len(servers)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(
            executor.map(lambda server: _probe_server(server, timeout), servers)
        )


def enrich_active_runs(
    servers: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(run.get("run_id")): run for run in runs}
    enriched: list[dict[str, Any]] = []
    for server in servers:
        item = dict(server)
        active_runs = []
        for active in server.get("active_runs", []):
            run_id = str(active["run_id"])
            known = by_id.get(run_id, {})
            active_runs.append(
                {
                    **active,
                    "controller_managed": bool(known),
                    **{
                        field: known[field]
                        for field in (
                            "label",
                            "task_id",
                            "authoritative_status",
                            "observation",
                            "progress",
                            "started_at",
                            "error",
                        )
                        if field in known
                    },
                }
            )
        item["active_runs"] = active_runs
        item["standard_runs"] = sum(
            run.get("workload_class") == "standard" for run in active_runs
        )
        item["test_runs"] = sum(
            run.get("workload_class") == "test" for run in active_runs
        )
        item["allocated_cores"] = sum(
            int(run["assigned_cores"])
            for run in active_runs
            if isinstance(run.get("assigned_cores"), int)
            and not isinstance(run["assigned_cores"], bool)
        )
        item["allocation_unknown"] = any(
            not isinstance(run.get("assigned_cores"), int)
            or isinstance(run["assigned_cores"], bool)
            for run in active_runs
        )
        enriched.append(item)
    return enriched
