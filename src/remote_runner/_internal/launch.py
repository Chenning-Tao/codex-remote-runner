from __future__ import annotations

import json
import subprocess
from typing import Any

from .execution_registry import (
    ProjectPaths,
    load_current_run,
    registry_kind,
    run_lock,
    update_current_state,
    utc_now,
    validate_current_run_id,
)
from .launch_plan import LaunchPlan, build_launch_plan


class BootstrapRejected(RuntimeError):
    pass


class BootstrapOutcomeUnknown(RuntimeError):
    pass


# Python, revision, and clean-tree checks can each consume 20s, followed by 5s startup proof.
REMOTE_PREFLIGHT_BUDGET_SECONDS = 65
PROCESS_TITLE_PREFLIGHT_BUDGET_SECONDS = 65
TRANSPORT_MARGIN_SECONDS = 10


def _bootstrap_result(stdout: bytes) -> dict[str, Any] | None:
    prefix = "RR_BOOTSTRAP_RESULT "
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        if not line.startswith(prefix):
            continue
        try:
            result = json.loads(line[len(prefix) :])
        except json.JSONDecodeError:
            return None
        return result if isinstance(result, dict) else None
    return None


def execute_plan(plan: LaunchPlan, timeout: int) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("launch timeout must be positive")
    argv = list(plan.bootstrap_ssh_argv)
    connect_timeout_index = argv.index("ConnectTimeout=8")
    argv[connect_timeout_index] = f"ConnectTimeout={timeout}"
    try:
        remote_budget = REMOTE_PREFLIGHT_BUDGET_SECONDS
        if plan.privacy_mode is not None:
            remote_budget += PROCESS_TITLE_PREFLIGHT_BUDGET_SECONDS
        completed = subprocess.run(
            argv,
            input=plan.bootstrap_stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + remote_budget + TRANSPORT_MARGIN_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapOutcomeUnknown(
            f"remote bootstrap timed out after {exc.timeout}s; launch outcome is unknown"
        ) from exc

    result = _bootstrap_result(completed.stdout)
    if completed.returncode == 0:
        if result is None or result.get("ok") is not True or result.get("tmux_started") is not True:
            raise BootstrapOutcomeUnknown(
                "remote bootstrap returned without a verified tmux-started result"
            )
        return result
    if result is not None and result.get("ok") is False and result.get("tmux_started") is False:
        raise BootstrapRejected(str(result.get("message") or "remote preflight rejected launch"))
    detail = completed.stderr.decode(errors="replace").strip() or completed.stdout.decode(
        errors="replace"
    ).strip()
    raise BootstrapOutcomeUnknown(
        (detail or f"remote bootstrap exited {completed.returncode}")
        + "; launch outcome is unknown"
    )


def _state_changes_from_remote(result: dict[str, Any]) -> dict[str, Any]:
    remote = result.get("status")
    started_at = remote.get("started_at") if isinstance(remote, dict) else None
    if not isinstance(started_at, str) or not started_at:
        started_at = utc_now()
    changes: dict[str, Any] = {
        "status": "running",
        "started_at": started_at,
        "error": None,
    }
    return changes


def launch(
    paths: ProjectPaths,
    run_id: str,
    timeout: int,
    *,
    dry_run: bool = False,
) -> tuple[dict[str, Any], LaunchPlan]:
    validate_current_run_id(run_id)
    if registry_kind(paths, run_id) != "current":
        raise ValueError("only current-format runs can be launched")
    with run_lock(paths, run_id):
        _manifest, state = load_current_run(paths, run_id)
        if state["status"] != "registered":
            raise ValueError(
                f"run {run_id} must be registered before launch; current status={state['status']!r}"
            )
        plan = build_launch_plan(paths, run_id)
        if dry_run:
            return state, plan
        try:
            result = execute_plan(plan, timeout)
        except BootstrapRejected as exc:
            state = update_current_state(
                paths,
                run_id,
                int(state["revision"]),
                {"status": "registered", "error": str(exc)},
                action="launch_rejected",
                lock_held=True,
            )
            raise RuntimeError(str(exc)) from exc
        except (BootstrapOutcomeUnknown, OSError) as exc:
            state = update_current_state(
                paths,
                run_id,
                int(state["revision"]),
                {"status": "registered", "error": str(exc)},
                action="launch_outcome_unknown",
                lock_held=True,
            )
            raise RuntimeError(str(exc)) from exc
        state = update_current_state(
            paths,
            run_id,
            int(state["revision"]),
            _state_changes_from_remote(result),
            action="launched",
            lock_held=True,
        )
        return state, plan
