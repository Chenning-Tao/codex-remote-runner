"""Source gate for derived validation runs.

Every fact this gate reads is already controller-local: the queue job, the
execution manifest, and the completed output-sync receipt. A source run therefore
never has to be re-probed over SSH to authorize a validator, and nothing here is
inferred from a label, output metadata, or caller-supplied text.

The gate answers one question only — may this exact source run be derived from,
and with which frozen identity. It never repairs a source record, and it never
interprets what the source produced.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from ..derivation import normalize_artifact_path, receipt_identity_sha256
from ..execution_registry import (
    load_current_run,
    project_paths,
    validate_current_run_id,
)
from ..output_sync import load_config, run_sync_status
from ..run_readiness import report_readiness
from .registry import ControllerPaths, load_job
from .run_view import load_run_view


CHECKSUM_VERIFICATION = "rsync_checksum_dry_run"


def source_facts(paths: ControllerPaths, source_run_id: str) -> dict[str, Any]:
    """Return the frozen identity a validator may be derived from, or fail closed."""
    run_id = validate_current_run_id(source_run_id)
    try:
        job, _queue_state = load_job(paths, run_id)
    except FileNotFoundError as exc:
        raise ValueError(f"source run {run_id} has no controller queue record") from exc
    if job.get("derivation") is not None:
        raise ValueError(
            f"source run {run_id} is itself a derived validation run; validators "
            "cannot be derived recursively"
        )

    view = load_run_view(paths, run_id)
    phase = view.get("phase")
    outcome = view.get("outcome")
    if phase != "terminal" or outcome != "succeeded":
        detail = view.get("attention_reason") or f"phase={phase} outcome={outcome}"
        raise ValueError(f"source run {run_id} did not succeed: {detail}")
    readiness = report_readiness(view)
    if readiness != "ready":
        raise ValueError(f"source run {run_id} is not reportable yet: {readiness}")

    if not paths.config_path.is_file():
        raise ValueError("controller project configuration is unavailable")
    manifest, _state = load_current_run(project_paths(paths.config_path), run_id)

    revision = str(job["revision"])
    execution_revision = manifest.get("source_revision") or manifest.get(
        "expected_revision"
    )
    if execution_revision != revision:
        raise ValueError(
            f"source run {run_id} queue and execution revisions disagree"
        )
    server = str(manifest["server"])

    sync = run_sync_status(paths.registry_root, run_id)
    if sync.get("status") != "completed":
        raise ValueError(
            f"source run {run_id} output sync is {sync.get('status')!r}, not completed"
        )
    receipt = sync.get("receipt")
    if not isinstance(receipt, dict):
        raise ValueError(f"source run {run_id} has no completed output-sync receipt")
    if receipt.get("verification") != CHECKSUM_VERIFICATION:
        raise ValueError(f"source run {run_id} artifact is not checksum verified")
    if receipt.get("run_id") != run_id:
        raise ValueError("output-sync receipt identity mismatch")
    if receipt.get("revision") != revision:
        raise ValueError(
            f"source run {run_id} receipt revision disagrees with the queued revision"
        )
    if receipt.get("authoritative_status") != "succeeded":
        raise ValueError(
            f"source run {run_id} receipt records a non-succeeded execution"
        )
    if receipt.get("source_server") != server:
        raise ValueError(
            f"source run {run_id} receipt server disagrees with the execution record"
        )

    config = load_config(paths.registry_root)
    if config is None:
        raise ValueError("controller output sync is not configured")
    target_path = normalize_artifact_path(
        receipt.get("target_path"), "output-sync receipt target_path"
    )
    target_root = PurePosixPath(config.target_root)
    if target_root not in PurePosixPath(target_path).parents:
        raise ValueError(
            "output-sync receipt target path is outside the configured archive root"
        )

    return {
        "source_run_id": run_id,
        "revision": revision,
        "server": server,
        "label": str(manifest["label"]),
        "task_id": str(manifest["task_id"]),
        "artifact": {
            "target_server": config.target_server,
            "target_path": target_path,
            "receipt_sha256": receipt_identity_sha256(receipt),
        },
    }
