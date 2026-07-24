from __future__ import annotations

import argparse
import math
import re
import sys
import time
from typing import Any, Callable

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config, validate_current_run_id


RUN_VIEW_SCHEMA_VERSION = 1
CONTROLLER_WAIT_SECONDS = 50
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
    if getattr(args, "until", "execution-terminal") != "execution-terminal":
        raise ValueError("only --until execution-terminal is currently supported")
    run_id = validate_current_run_id(args.run_id)
    timeout = _validate_positive(args.timeout, "--timeout")
    connection_grace = _validate_positive(
        getattr(args, "connection_grace", 300),
        "--connection-grace",
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
    reported_phase: str | None = None

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
            if failed_at - first_transport_error_at >= connection_grace:
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
        if phase != reported_phase:
            detail = f" outcome={view['outcome']}" if view.get("outcome") else ""
            reporter(f"[remote-runner wait] {run_id} phase={phase}{detail}")
            reported_phase = phase
        elif payload.get("timed_out") is True:
            elapsed = max(0.0, time.monotonic() - started_at)
            reporter(
                f"[remote-runner wait] {run_id} heartbeat "
                f"phase={phase} elapsed={elapsed:.0f}s"
            )

        if phase == TERMINAL_PHASE:
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
