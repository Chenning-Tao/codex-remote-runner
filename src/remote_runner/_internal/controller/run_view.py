from __future__ import annotations

import json
from typing import Any

from .. import monitoring
from ..execution_registry import (
    project_paths,
    sha256_bytes,
    validate_current_run_id,
)
from ..output_sync import run_sync_status
from .registry import ControllerPaths, load_job, load_run_tombstone


RUN_VIEW_SCHEMA_VERSION = 1
EXECUTION_TERMINAL = {"succeeded", "failed", "stopped"}
QUEUE_TERMINAL = {"failed", "stopped"}
ACTIVE_PHASES = {"queued", "dispatching", "registered", "running"}
UNKNOWN_LAUNCH_ATTENTION = "execution launch outcome remains unknown"
RUNTIME_ABSENT_ATTENTION = (
    "remote runtime is absent while execution authority remains active"
)


def _queue_projection(
    job: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": state["status"],
        "revision": state["revision"],
        "updated_at": state["updated_at"],
        "error": state.get("error"),
        "label": job["label"],
        "task_id": job["task_id"],
        "result_intent": job["result_intent"],
        "workload_class": job["workload_class"],
    }


def _execution_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "registry_kind",
            "authoritative_status",
            "stored_status",
            "revision",
            "updated_at",
            "started_at",
            "finished_at",
            "exit_code",
            "error",
            "server",
            "label",
            "task_id",
            "result_intent",
            "workload_class",
        )
    }


def _derive_phase(
    queue: dict[str, Any] | None,
    execution: dict[str, Any] | None,
    purge: dict[str, Any] | None,
) -> tuple[str, str | None, str | None, str | None]:
    queue_status = None if queue is None else queue.get("status")
    execution_kind = None if execution is None else execution.get("registry_kind")
    execution_status = (
        None if execution is None else execution.get("authoritative_status")
    )

    if execution_kind == "current" and execution_status in EXECUTION_TERMINAL:
        if queue_status in QUEUE_TERMINAL and queue_status != execution_status:
            return (
                "attention_required",
                None,
                None,
                "queue and execution terminal authorities conflict",
            )
        return "terminal", str(execution_status), "execution", None

    if execution_kind == "current" and execution_status in {"registered", "running"}:
        if queue_status in QUEUE_TERMINAL:
            return (
                "attention_required",
                None,
                None,
                "queue is terminal while the execution remains active",
            )
        execution_error = None if execution is None else execution.get("error")
        if execution_error == monitoring.RUNTIME_ABSENT_ERROR:
            return "attention_required", None, None, RUNTIME_ABSENT_ATTENTION
        if isinstance(execution_error, str) and "launch outcome is unknown" in (
            execution_error.lower()
        ):
            return "attention_required", None, None, UNKNOWN_LAUNCH_ATTENTION
        return str(execution_status), None, None, None

    if execution is not None:
        return (
            "attention_required",
            None,
            None,
            "execution record is not a supported current authoritative record",
        )

    if queue_status in QUEUE_TERMINAL:
        return "terminal", str(queue_status), "queue", None
    if queue_status == "queued":
        return "queued", None, None, None
    if queue_status == "dispatching":
        return "dispatching", None, None, None
    if queue_status == "dispatched":
        return (
            "attention_required",
            None,
            None,
            "queue is dispatched but no execution record is available",
        )
    if queue is not None:
        return "attention_required", None, None, "queue status is unsupported"
    if purge is not None:
        return "purged", None, None, None
    return "missing", None, None, None


def derive_run_view(
    *,
    project_id: str,
    run_id: str,
    queue: dict[str, Any] | None,
    execution: dict[str, Any] | None,
    output_sync: dict[str, Any],
    purge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    phase, outcome, terminal_source, attention_reason = _derive_phase(
        queue,
        execution,
        purge,
    )
    view: dict[str, Any] = {
        "schema_version": RUN_VIEW_SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "phase": phase,
        "outcome": outcome,
        "terminal_source": terminal_source,
        "queue": queue,
        "execution": execution,
        "output_sync": output_sync,
        "purge": purge,
    }
    if attention_reason is not None:
        view["attention_reason"] = attention_reason
    canonical = json.dumps(
        view,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    view["etag"] = sha256_bytes(canonical)
    return view


def load_run_view(paths: ControllerPaths, run_id: str) -> dict[str, Any]:
    validated = validate_current_run_id(run_id)
    try:
        job, queue_state = load_job(paths, validated)
    except FileNotFoundError:
        queue = None
    else:
        queue = _queue_projection(job, queue_state)

    execution = None
    if paths.config_path.is_file():
        rows = monitoring.load_registry_rows(
            project_paths(paths.config_path),
            only_run_id=validated,
        )
        if len(rows) > 1:
            raise RuntimeError(f"multiple execution records exist for {validated}")
        if rows:
            execution = _execution_projection(rows[0])

    return derive_run_view(
        project_id=paths.project_id,
        run_id=validated,
        queue=queue,
        execution=execution,
        output_sync=run_sync_status(paths.registry_root, validated),
        purge=load_run_tombstone(paths, validated),
    )
