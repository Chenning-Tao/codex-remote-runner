from __future__ import annotations

import argparse
import math
import re
import sys
import time
from typing import Any, Callable

from .config import load_managed_project_config
from .controller.client import ControllerActionUnsupportedError, call_controller
from .execution_registry import resolve_project_config, validate_current_run_id
from .run_readiness import output_sync_status, report_readiness


RUN_VIEW_SCHEMA_VERSION = 1
CONTROLLER_WAIT_SECONDS = 50
MAX_COHORT_RUNS = 64
LEGACY_TERMINAL_BACKOFF_SECONDS = 10
TERMINAL_PHASE = "terminal"
STOP_PHASES = {"attention_required", "missing", "purged"}
ETAG_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
Reporter = Callable[[str], None]


def _stderr_report(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _validate_positive(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _run_view(payload: dict[str, Any], expected_run_id: str) -> dict[str, Any]:
    view = payload.get("run_view")
    if not isinstance(view, dict):
        raise RuntimeError("controller response has no run_view")
    if view.get("schema_version") != RUN_VIEW_SCHEMA_VERSION:
        raise RuntimeError("controller returned an unsupported run_view schema")
    if view.get("run_id") != expected_run_id:
        raise RuntimeError("controller run_view identity mismatch")
    etag = view.get("etag")
    if not isinstance(etag, str) or ETAG_RE.fullmatch(etag) is None:
        raise RuntimeError("controller run_view has an invalid etag")
    phase = view.get("phase")
    if phase not in {
        "queued",
        "dispatching",
        "registered",
        "running",
        "terminal",
        "attention_required",
        "missing",
        "purged",
    }:
        raise RuntimeError(f"controller run_view has an invalid phase: {phase!r}")
    if phase == TERMINAL_PHASE and view.get("outcome") not in {
        "succeeded",
        "failed",
        "stopped",
    }:
        raise RuntimeError("terminal run_view has no valid outcome")
    return view


def _result(
    *,
    wait_status: str,
    started_at: float,
    controller_calls: int,
    transport_retries: int,
    view: dict[str, Any],
) -> dict[str, Any]:
    return {
        "wait_status": wait_status,
        "elapsed_seconds": round(max(0.0, time.monotonic() - started_at), 3),
        "controller_calls": controller_calls,
        "transport_retries": transport_retries,
        "run_view": view,
    }


def _cohort_result(
    *,
    wait_status: str,
    started_at: float,
    controller_calls: int,
    transport_retries: int,
    views: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "wait_status": wait_status,
        "elapsed_seconds": round(max(0.0, time.monotonic() - started_at), 3),
        "controller_calls": controller_calls,
        "transport_retries": transport_retries,
        "run_views": views,
    }


def _cohort_views(
    payload: dict[str, Any], expected_run_ids: list[str]
) -> list[dict[str, Any]]:
    raw_views = payload.get("run_views")
    if not isinstance(raw_views, list):
        raise RuntimeError("controller response has no run_views")
    if len(raw_views) != len(expected_run_ids):
        raise RuntimeError("controller run_views count mismatch")
    views = [
        _run_view({"run_view": raw_view}, run_id)
        for raw_view, run_id in zip(raw_views, expected_run_ids, strict=True)
        if isinstance(raw_view, dict)
    ]
    if len(views) != len(expected_run_ids):
        raise RuntimeError("controller returned an invalid cohort run_view")
    return views


def _cohort_requires_attention(views: list[dict[str, Any]]) -> bool:
    for view in views:
        phase = view["phase"]
        if phase in STOP_PHASES:
            return True
        if phase == TERMINAL_PHASE:
            if view.get("outcome") in {"failed", "stopped"}:
                return True
            if report_readiness(view) == "attention":
                return True
    return False


def wait_exit_code(result: dict[str, Any]) -> int:
    status = result.get("wait_status")
    if status == "completed":
        return 0
    if status == "timed_out":
        return 3
    return 4


def wait_for_run(
    args: argparse.Namespace,
    *,
    reporter: Reporter = _stderr_report,
) -> dict[str, Any]:
    until = getattr(args, "until", "execution-terminal")
    if until not in {"execution-terminal", "reportable"}:
        raise ValueError("--until must be execution-terminal or reportable")
    run_id = validate_current_run_id(args.run_id)
    timeout = _validate_positive(args.timeout, "--timeout")
    connection_grace_value = getattr(args, "connection_grace", None)
    connection_grace = (
        None
        if connection_grace_value is None
        else _validate_positive(connection_grace_value, "--connection-grace")
    )
    max_wait_value = getattr(args, "max_wait", None)
    max_wait = (
        None
        if max_wait_value is None
        else _validate_positive(max_wait_value, "--max-wait")
    )
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)

    started_at = time.monotonic()
    deadline = None if max_wait is None else started_at + max_wait
    first_transport_error_at: float | None = None
    transport_retries = 0
    controller_calls = 0
    view: dict[str, Any] | None = None
    reported_state: tuple[str, object, str] | None = None

    while True:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            if view is None:
                raise RuntimeError(
                    "--max-wait expired before the controller returned a run state"
                )
            return _result(
                wait_status="timed_out",
                started_at=started_at,
                controller_calls=controller_calls,
                transport_retries=transport_retries,
                view=view,
            )

        if view is None:
            action = "status"
            action_args = ("--run-id", run_id)
            overall_timeout = None
        else:
            wait_seconds = CONTROLLER_WAIT_SECONDS
            if deadline is not None:
                wait_seconds = min(
                    wait_seconds,
                    max(0, math.ceil(deadline - now)),
                )
            action = "wait-run"
            action_args = (
                "--run-id",
                run_id,
                "--after-etag",
                str(view["etag"]),
                "--wait-seconds",
                str(wait_seconds),
            )
            overall_timeout = timeout + wait_seconds + 10

        try:
            controller_calls += 1
            payload = call_controller(
                config,
                action,
                timeout=timeout,
                action_args=action_args,
                overall_timeout=overall_timeout,
            )
        except RuntimeError as exc:
            failed_at = time.monotonic()
            if first_transport_error_at is None:
                first_transport_error_at = failed_at
            if (
                connection_grace is not None
                and failed_at - first_transport_error_at >= connection_grace
            ):
                raise RuntimeError(
                    "controller remained unavailable beyond --connection-grace; "
                    f"last error: {exc}"
                ) from exc
            transport_retries += 1
            delay = min(30, 2 ** min(transport_retries - 1, 5))
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - failed_at))
            reporter(
                f"[remote-runner wait] {run_id} controller unavailable; "
                f"retrying in {delay:g}s"
            )
            time.sleep(delay)
            continue

        first_transport_error_at = None
        view = _run_view(payload, run_id)
        phase = str(view["phase"])
        sync_status = output_sync_status(view)
        state = (phase, view.get("outcome"), sync_status)
        if state != reported_state:
            detail = f" outcome={view['outcome']}" if view.get("outcome") else ""
            if phase == TERMINAL_PHASE and view.get("outcome") == "succeeded":
                detail += f" output_sync={sync_status}"
            reporter(f"[remote-runner wait] {run_id} phase={phase}{detail}")
            reported_state = state
        elif payload.get("timed_out") is True:
            elapsed = max(0.0, time.monotonic() - started_at)
            reporter(
                f"[remote-runner wait] {run_id} heartbeat "
                f"phase={phase} elapsed={elapsed:.0f}s"
            )

        if phase == TERMINAL_PHASE:
            if until == "reportable":
                readiness = report_readiness(view)
                if readiness == "waiting":
                    if (
                        payload.get("changed") is False
                        and payload.get("timed_out") is False
                    ):
                        delay = LEGACY_TERMINAL_BACKOFF_SECONDS
                        if deadline is not None:
                            delay = min(delay, max(0.0, deadline - time.monotonic()))
                        if delay > 0:
                            time.sleep(delay)
                    continue
                if readiness == "attention":
                    return _result(
                        wait_status="attention_required",
                        started_at=started_at,
                        controller_calls=controller_calls,
                        transport_retries=transport_retries,
                        view=view,
                    )
            return _result(
                wait_status="completed",
                started_at=started_at,
                controller_calls=controller_calls,
                transport_retries=transport_retries,
                view=view,
            )
        if phase in STOP_PHASES:
            return _result(
                wait_status=phase,
                started_at=started_at,
                controller_calls=controller_calls,
                transport_retries=transport_retries,
                view=view,
            )


def wait_for_cohort(
    args: argparse.Namespace,
    *,
    reporter: Reporter = _stderr_report,
) -> dict[str, Any]:
    until = getattr(args, "until", "reportable")
    if until != "reportable":
        raise ValueError("wait-cohort only supports --until reportable")
    raw_run_ids = getattr(args, "run_ids", None)
    if not isinstance(raw_run_ids, list) or not raw_run_ids:
        raise ValueError("wait-cohort requires at least one --run-id")
    if len(raw_run_ids) > MAX_COHORT_RUNS:
        raise ValueError(
            f"wait-cohort accepts at most {MAX_COHORT_RUNS} --run-id values"
        )
    run_ids = [validate_current_run_id(run_id) for run_id in raw_run_ids]
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("wait-cohort requires unique --run-id values")

    timeout = _validate_positive(args.timeout, "--timeout")
    connection_grace_value = getattr(args, "connection_grace", None)
    connection_grace = (
        None
        if connection_grace_value is None
        else _validate_positive(connection_grace_value, "--connection-grace")
    )
    max_wait_value = getattr(args, "max_wait", None)
    max_wait = (
        None
        if max_wait_value is None
        else _validate_positive(max_wait_value, "--max-wait")
    )
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)

    started_at = time.monotonic()
    deadline = None if max_wait is None else started_at + max_wait
    first_transport_error_at: float | None = None
    transport_retries = 0
    controller_calls = 0
    views: list[dict[str, Any]] = []

    while True:
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            if not views:
                raise RuntimeError(
                    "--max-wait expired before the controller returned cohort state"
                )
            return _cohort_result(
                wait_status="timed_out",
                started_at=started_at,
                controller_calls=controller_calls,
                transport_retries=transport_retries,
                views=views,
            )

        wait_seconds = 0 if not views else CONTROLLER_WAIT_SECONDS
        if deadline is not None:
            wait_seconds = min(
                wait_seconds,
                max(0, math.ceil(deadline - now)),
            )
        payload = {
            "schema_version": 1,
            "wait_seconds": wait_seconds,
            "runs": [
                {
                    "run_id": run_id,
                    "after_etag": None if not views else view["etag"],
                }
                for run_id, view in zip(run_ids, views or [{}] * len(run_ids), strict=True)
            ],
        }

        try:
            controller_calls += 1
            response = call_controller(
                config,
                "wait-runs",
                timeout=timeout,
                payload=payload,
                overall_timeout=timeout + wait_seconds + 10,
            )
        except ControllerActionUnsupportedError as exc:
            raise RuntimeError(
                "controller does not support wait-cohort; upgrade the controller "
                "before using cohort waits"
            ) from exc
        except RuntimeError as exc:
            failed_at = time.monotonic()
            if first_transport_error_at is None:
                first_transport_error_at = failed_at
            if (
                connection_grace is not None
                and failed_at - first_transport_error_at >= connection_grace
            ):
                raise RuntimeError(
                    "controller remained unavailable beyond --connection-grace; "
                    f"last error: {exc}"
                ) from exc
            transport_retries += 1
            delay = min(30, 2 ** min(transport_retries - 1, 5))
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - failed_at))
            reporter(
                "[remote-runner wait-cohort] controller unavailable; "
                f"retrying in {delay:g}s"
            )
            time.sleep(delay)
            continue

        first_transport_error_at = None
        views = _cohort_views(response, run_ids)
        if _cohort_requires_attention(views):
            return _cohort_result(
                wait_status="attention_required",
                started_at=started_at,
                controller_calls=controller_calls,
                transport_retries=transport_retries,
                views=views,
            )
        if all(report_readiness(view) == "ready" for view in views):
            return _cohort_result(
                wait_status="completed",
                started_at=started_at,
                controller_calls=controller_calls,
                transport_retries=transport_retries,
                views=views,
            )
