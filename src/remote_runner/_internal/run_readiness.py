from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal


ReportReadiness = Literal["waiting", "ready", "attention"]
ATTENTION_PHASES = {"attention_required", "missing", "purged"}
OUTPUT_SYNC_WAITING_STATUSES = {
    "pending",
    "retryable",
    "waiting_for_succeeded_state",
}
OUTPUT_SYNC_ATTENTION_STATUSES = {"cancelled", "unknown"}
OUTPUT_SYNC_READY_STATUSES = {"completed", "not_enqueued"}


def output_sync_status(view: dict[str, Any]) -> str:
    output_sync = view.get("output_sync")
    if not isinstance(output_sync, dict):
        return "unknown"
    status = output_sync.get("status")
    return status if isinstance(status, str) else "unknown"


def report_readiness(view: dict[str, Any]) -> ReportReadiness:
    phase = view.get("phase")
    if phase in ATTENTION_PHASES:
        return "attention"
    if phase != "terminal":
        return "waiting"
    outcome = view.get("outcome")
    if outcome in {"failed", "stopped"}:
        return "ready"
    if outcome != "succeeded":
        return "attention"
    sync_status = output_sync_status(view)
    if sync_status in OUTPUT_SYNC_WAITING_STATUSES:
        return "waiting"
    if sync_status in OUTPUT_SYNC_READY_STATUSES:
        return "ready"
    return "attention"


def cohort_report_readiness(views: Sequence[dict[str, Any]]) -> ReportReadiness:
    states = [report_readiness(view) for view in views]
    if any(state == "attention" for state in states):
        return "attention"
    if states and all(state == "ready" for state in states):
        return "ready"
    return "waiting"
