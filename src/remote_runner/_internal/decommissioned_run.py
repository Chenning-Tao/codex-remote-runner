from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from . import monitoring
from .config import load_managed_project_config
from .controller.client import call_controller
from .controller.registry import (
    ControllerPaths,
    controller_scheduler_paths,
    load_job,
    load_server_lease,
    scheduler_lock,
)
from .execution_registry import (
    TERMINAL_STATUSES,
    load_current_run,
    project_paths,
    registry_kind,
    resolve_project_config,
    run_lock,
    update_current_state,
    utc_now,
    validate_current_run_id,
)
from .output_sync import is_configured, run_sync_status


REQUEST_SCHEMA = 1
MAX_REASON_LENGTH = 500


def _reason(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("decommissioned-run reason must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError("decommissioned-run reason must not be empty")
    if len(normalized) > MAX_REASON_LENGTH:
        raise ValueError(
            f"decommissioned-run reason must be at most {MAX_REASON_LENGTH} characters"
        )
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("decommissioned-run reason must not contain control characters")
    return normalized


def validate_request(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "server",
        "reason",
    }:
        raise ValueError("decommissioned-run request payload is invalid")
    if payload.get("schema_version") != REQUEST_SCHEMA:
        raise ValueError("unsupported decommissioned-run request schema")
    server = payload.get("server")
    if not isinstance(server, str) or not server.strip():
        raise ValueError("decommissioned-run server must be a non-empty string")
    return {"server": server.strip(), "reason": _reason(payload.get("reason"))}


def _leases(paths: ControllerPaths) -> list[dict[str, Any]]:
    scheduler = controller_scheduler_paths(paths.root)
    if not scheduler.leases_dir.is_dir():
        return []
    return [
        load_server_lease(path)
        for path in sorted(scheduler.leases_dir.glob("*.yaml"))
    ]


def _matching_leases(
    paths: ControllerPaths,
    manifest: dict[str, Any],
    leases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        lease
        for lease in leases
        if lease["run_id"] == manifest["run_id"]
        or lease["machine_id"] == manifest["machine_id"]
        or (
            lease["project_id"] == paths.project_id
            and lease["server"] == manifest["server"]
        )
    ]


def _blocking_leases(
    paths: ControllerPaths,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    with scheduler_lock(paths.root):
        return _matching_leases(paths, manifest, _leases(paths))


def _probe_summary(probe: dict[str, Any]) -> dict[str, Any]:
    return {
        "observation": probe.get("observation"),
        "ssh_reachable": probe.get("ssh_reachable"),
        "error": probe.get("error"),
    }


def _validate_output_sync(
    registry_root: Path,
    manifest: dict[str, Any],
    run_id: str,
) -> None:
    if is_configured(registry_root) and manifest.get("output_path") is not None:
        raise RuntimeError(
            f"run {run_id} has configured output synchronization; "
            "retire or archive its output contract before closing it"
        )
    sync = run_sync_status(registry_root, run_id)
    if sync.get("status") != "not_enqueued":
        raise RuntimeError(
            f"run {run_id} has output synchronization state and cannot be "
            "closed as a decommissioned-server record"
        )


def inspect_or_close(
    paths: ControllerPaths,
    run_id: str,
    *,
    server: str,
    reason: str,
    timeout: int,
    apply: bool,
) -> dict[str, Any]:
    validated = validate_current_run_id(run_id)
    normalized_reason = _reason(reason)
    if timeout <= 0:
        raise ValueError("decommissioned-run timeout must be positive")

    try:
        _job, queue_state = load_job(paths, validated)
    except FileNotFoundError:
        queue_state = None
    if queue_state is not None and queue_state["status"] != "dispatched":
        raise RuntimeError(
            f"run {validated} is still {queue_state['status']}; "
            "it is not eligible for decommissioned-server closure"
        )
    if not paths.config_path.is_file():
        raise FileNotFoundError(
            f"controller project config is missing for {paths.project_id}"
        )
    execution_paths = project_paths(paths.config_path)
    if registry_kind(execution_paths, validated) != "current":
        raise ValueError(
            f"only current-format runs can be closed after decommissioning: {validated}"
        )

    with run_lock(execution_paths, validated):
        manifest, state = load_current_run(execution_paths, validated)
        if manifest["server"] != server:
            raise ValueError(
                f"run {validated} belongs to server {manifest['server']!r}, not {server!r}"
            )
        if state["status"] in TERMINAL_STATUSES:
            return {
                "schema_version": REQUEST_SCHEMA,
                "run_id": validated,
                "server": server,
                "status": "already_terminal",
                "applied": False,
                "state": state,
            }

    leases = _blocking_leases(paths, manifest)
    if leases:
        raise RuntimeError(
            f"run {validated} or its physical server still has an active controller lease"
        )
    _validate_output_sync(execution_paths.registry_root, manifest, validated)

    rows = monitoring.load_registry_rows(
        execution_paths,
        only_run_id=validated,
        active_only=False,
    )
    if len(rows) != 1 or rows[0].get("registry_kind") != "current":
        raise RuntimeError(
            f"run {validated} could not be loaded for a decommissioned-server probe"
        )
    probe = monitoring.remote_probe(rows[0], timeout)
    if (
        probe.get("observation") != "unreachable"
        or probe.get("ssh_reachable") is not False
    ):
        raise RuntimeError(
            f"run {validated} remote endpoint is not proven unreachable; "
            "use the normal monitor/stop lifecycle"
        )

    preview = {
        "schema_version": REQUEST_SCHEMA,
        "run_id": validated,
        "server": server,
        "reason": normalized_reason,
        "status": "ready_to_close",
        "applied": False,
        "state_revision": int(state["revision"]),
        "authoritative_status": state["status"],
        "probe": _probe_summary(probe),
        "preserved": [
            "controller queue record",
            "controller execution manifest and event history",
            "remote runtime and output bytes",
        ],
    }
    if not apply:
        return preview

    with scheduler_lock(paths.root):
        leases = _matching_leases(paths, manifest, _leases(paths))
        if leases:
            raise RuntimeError(
                f"run {validated} or its physical server acquired a controller lease"
            )
        with run_lock(execution_paths, validated):
            current_manifest, current = load_current_run(
                execution_paths,
                validated,
            )
            if current_manifest["server"] != server:
                raise ValueError(
                    f"run {validated} changed server identity during closure"
                )
            if current["status"] in TERMINAL_STATUSES:
                return {
                    "schema_version": REQUEST_SCHEMA,
                    "run_id": validated,
                    "server": server,
                    "status": "already_terminal",
                    "applied": False,
                    "state": current,
                }
            _validate_output_sync(
                execution_paths.registry_root,
                current_manifest,
                validated,
            )
            updated = update_current_state(
                execution_paths,
                validated,
                int(current["revision"]),
                {
                    "status": "stopped",
                    "finished_at": utc_now(),
                    "exit_code": None,
                    "error": f"server decommissioned: {normalized_reason}",
                },
                action="decommissioned_server_closed",
                lock_held=True,
            )
        return {
            **preview,
            "status": "closed",
            "applied": True,
            "state": updated,
        }


def request_close(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    action_args = ["--run-id", args.run_id]
    if args.apply:
        action_args.append("--apply")
    return call_controller(
        config,
        "close-decommissioned-run",
        timeout=args.timeout,
        action_args=tuple(action_args),
        payload={
            "schema_version": REQUEST_SCHEMA,
            "server": args.server,
            "reason": _reason(args.reason),
        },
    )
