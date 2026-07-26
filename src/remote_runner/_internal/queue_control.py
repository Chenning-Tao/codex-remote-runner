from __future__ import annotations

import argparse
from typing import Any

from . import server_addition
from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config, validate_current_run_id


class QueuePreparationError(RuntimeError):
    pass


def _prepared_server_names(value: dict[str, Any], run_id: str) -> list[str]:
    job = value.get("job")
    if not isinstance(job, dict) or job.get("run_id") != run_id:
        raise RuntimeError("controller returned an invalid queued job")
    prepared = job.get("prepared_servers")
    if (
        not isinstance(prepared, list)
        or not prepared
        or any(not isinstance(name, str) for name in prepared)
    ):
        raise RuntimeError("controller returned invalid prepared servers")
    return prepared


def _reservation(value: dict[str, Any]) -> tuple[str, int]:
    token = value.get("token")
    state = value.get("state")
    revision = state.get("revision") if isinstance(state, dict) else None
    if (
        not isinstance(token, str)
        or not token
        or isinstance(revision, bool)
        or not isinstance(revision, int)
    ):
        raise RuntimeError("controller returned an invalid queue update reservation")
    return token, revision


def request_queue_update(
    args: argparse.Namespace,
    run_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    validated_run_id = validate_current_run_id(run_id)
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    selected = payload.get("eligible_servers")
    workload_class = payload.get("workload_class")
    if workload_class == "test" and isinstance(selected, list):
        outside = [
            name for name in selected if name not in config.scheduling.testing_servers
        ]
        if outside:
            raise ValueError(
                "test workload servers are outside the configured testing pool: "
                + ", ".join(outside)
            )
    if not isinstance(selected, list):
        return call_controller(
            config,
            "update-queued-job",
            timeout=args.timeout,
            action_args=("--run-id", validated_run_id),
            payload=payload,
        )

    prepared = _prepared_server_names(
        call_controller(
            config,
            "queued-job",
            timeout=args.timeout,
            action_args=("--run-id", validated_run_id),
        ),
        validated_run_id,
    )
    missing = [name for name in selected if name not in set(prepared)]
    if not missing:
        return call_controller(
            config,
            "update-queued-job",
            timeout=args.timeout,
            action_args=("--run-id", validated_run_id),
            payload=payload,
        )

    prepare_timeout = int(getattr(args, "prepare_timeout", 60))
    ttl_seconds = min(3600, max(60, prepare_timeout * len(missing) + 60))
    token, reserved_revision = _reservation(
        call_controller(
            config,
            "reserve-queue-update",
            timeout=args.timeout,
            action_args=("--run-id", validated_run_id),
            payload={
                "expected_revision": payload["expected_revision"],
                "requested_servers": selected,
                "ttl_seconds": ttl_seconds,
            },
        )
    )

    try:
        for server in missing:
            addition_args = argparse.Namespace(**vars(args))
            addition_args.run_id = validated_run_id
            addition_args.server = server
            addition_args.placement_token = token
            addition_args.target_workload_class = workload_class
            server_addition.add(addition_args)
        committed_payload = {
            **payload,
            "expected_revision": reserved_revision,
            "placement_token": token,
        }
        return call_controller(
            config,
            "update-queued-job",
            timeout=args.timeout,
            action_args=("--run-id", validated_run_id),
            payload=committed_payload,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            call_controller(
                config,
                "release-queue-update",
                timeout=args.timeout,
                action_args=("--run-id", validated_run_id),
                payload={"token": token},
            )
        except (OSError, RuntimeError, ValueError):
            pass
        raise QueuePreparationError(str(exc)) from exc
