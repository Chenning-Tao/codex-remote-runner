from __future__ import annotations

import argparse
from pathlib import PurePosixPath
from typing import Any

from ..execution_registry import load_current_run, project_paths, registry_kind
from ..output_prune import OutputPruneOutcomeUnknown, prune_remote_output
from ..output_sync import (
    list_completed_syncs,
    record_source_output_deletion,
    validate_intent,
)
from .registry import (
    acquire_maintenance_lease,
    controller_paths,
    release_dispatch_lease,
)


def _overlaps(first: str, second: str) -> bool:
    left = PurePosixPath(first)
    right = PurePosixPath(second)
    return left == right or left in right.parents or right in left.parents


def _candidate(
    completed: dict[str, Any],
    *,
    manifest: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(completed.get("run_id"))
    if completed.get("schema_version") != 1:
        raise ValueError("unsupported completed output-sync schema")
    raw_intent = completed.get("intent")
    intent = validate_intent(
        dict(raw_intent) if isinstance(raw_intent, dict) else raw_intent
    )
    receipt = completed.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise ValueError("completed output-sync receipt is invalid")
    if receipt.get("run_id") != run_id or intent.get("run_id") != run_id:
        raise ValueError("completed output-sync identity mismatch")
    if receipt.get("verification") != "rsync_checksum_dry_run":
        raise ValueError("source output lacks checksum verification")
    if receipt.get("source_kind") not in {"file", "directory"}:
        raise ValueError("source output receipt has invalid source kind")
    for field in ("source_server", "source_path", "revision"):
        if receipt.get(field) != intent.get(field):
            raise ValueError(f"output-sync receipt {field} does not match intent")
    if not isinstance(receipt.get("target_path"), str):
        raise ValueError("output-sync receipt has no archived target path")
    if not isinstance(receipt.get("source_deletion_performed"), bool):
        raise ValueError("output-sync receipt has invalid source deletion state")
    if state.get("status") not in {"succeeded", "failed", "stopped"}:
        raise ValueError("run is not authoritatively terminal")
    if state.get("status") != intent.get("authoritative_status"):
        raise ValueError("run status differs from synchronized intent")
    if manifest.get("server") != intent["source_server"]:
        raise ValueError("run source server differs from synchronized intent")
    if manifest.get("output_path") != intent["source_path"]:
        raise ValueError("run output path differs from synchronized intent")
    revision = manifest.get("source_revision") or manifest.get("expected_revision")
    if revision is not None and revision != intent["revision"]:
        raise ValueError("run source revision differs from synchronized intent")
    for field in ("ssh", "project_python", "remote_workdir"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise ValueError(f"run manifest has no valid {field}")
    output_root = manifest.get("output_root")
    if not isinstance(output_root, str) or not output_root:
        raise ValueError("run manifest has no configured output_root")
    return {
        "run_id": run_id,
        "server": intent["source_server"],
        "machine_id": manifest.get("machine_id", intent["source_server"]),
        "source_path": intent["source_path"],
        "archive_path": receipt["target_path"],
        "archived_at": receipt.get("archived_at"),
        "already_pruned": receipt["source_deletion_performed"],
        "ssh": manifest["ssh"],
        "python": manifest["project_python"],
        "output_root": output_root,
        "remote_workdir": manifest["remote_workdir"],
    }


def _public(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in (
            "run_id",
            "server",
            "source_path",
            "archive_path",
            "archived_at",
        )
    }


def _load_current_runs(
    execution_paths: Any,
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    current: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    if not execution_paths.runs_dir.is_dir():
        return current
    for entry in execution_paths.runs_dir.iterdir():
        if (
            not entry.is_dir()
            or registry_kind(execution_paths, entry.name) != "current"
        ):
            continue
        current[entry.name] = load_current_run(execution_paths, entry.name)
    return current


def _overlap_blockers(
    candidate: dict[str, Any],
    current: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    pruned_ids: set[str],
) -> list[str]:
    blockers: list[str] = []
    for other_id, (manifest, _state) in current.items():
        if other_id == candidate["run_id"] or other_id in pruned_ids:
            continue
        other_path = manifest.get("output_path")
        if not isinstance(other_path, str):
            continue
        same_source = (
            manifest.get("server") == candidate["server"]
            or manifest.get("ssh") == candidate["ssh"]
        )
        if same_source and _overlaps(str(candidate["source_path"]), other_path):
            blockers.append(other_id)
    return sorted(blockers)


def prune_outputs(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    if not paths.config_path.is_file():
        raise ValueError("controller project config is unavailable")
    execution_paths = project_paths(paths.config_path)
    requested = args.run_id
    requested_servers = set(getattr(args, "server", None) or ())
    completed = {
        str(item["run_id"]): item
        for item in list_completed_syncs(execution_paths.registry_root)
    }
    if requested is not None and requested not in completed:
        raise ValueError(f"run has no completed output synchronization: {requested}")

    current = _load_current_runs(execution_paths)

    candidates: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []
    already_pruned: list[str] = []
    for run_id, record in sorted(completed.items()):
        if requested is not None and run_id != requested:
            continue
        raw_intent = record.get("intent")
        source_server = (
            raw_intent.get("source_server") if isinstance(raw_intent, dict) else None
        )
        if requested_servers and source_server not in requested_servers:
            continue
        try:
            run = current.get(run_id)
            if run is None:
                raise ValueError("current run record is unavailable")
            candidate = _candidate(record, manifest=run[0], state=run[1])
        except (KeyError, TypeError, ValueError) as exc:
            if requested is not None:
                raise ValueError(
                    f"run is not eligible for output pruning: {exc}"
                ) from exc
            blocked.append({"run_id": run_id, "reason": str(exc)})
            continue
        if candidate["already_pruned"]:
            already_pruned.append(run_id)
        else:
            candidates.append(candidate)

    pruned_ids = set(already_pruned)
    eligible: list[dict[str, Any]] = []
    for candidate in candidates:
        blockers = _overlap_blockers(candidate, current, pruned_ids=pruned_ids)
        if blockers:
            reason = "output path overlaps retained runs: " + ", ".join(
                sorted(blockers)
            )
            if requested is not None:
                raise ValueError(reason)
            blocked.append({"run_id": str(candidate["run_id"]), "reason": reason})
        else:
            eligible.append(candidate)

    if not args.apply:
        return {
            "applied": False,
            "servers": sorted(requested_servers),
            "candidate_count": len(eligible),
            "candidates": [_public(item) for item in eligible],
            "already_pruned": already_pruned,
            "blocked": blocked,
        }

    results: list[dict[str, Any]] = []
    for candidate in eligible:
        run_id = str(candidate["run_id"])
        result: dict[str, Any] = {"run_id": run_id, "status": "pruned"}
        leased = acquire_maintenance_lease(
            paths,
            server=str(candidate["server"]),
            machine_id=str(candidate["machine_id"]),
            run_id=run_id,
            ttl_seconds=600,
        )
        if not leased:
            result.update(
                {
                    "status": "failed",
                    "error": f"server {candidate['server']} lease is busy",
                }
            )
            results.append(result)
            continue
        try:
            refreshed = _load_current_runs(execution_paths)
            run = refreshed.get(run_id)
            if run is None:
                raise ValueError("current run record disappeared before output pruning")
            _candidate(completed[run_id], manifest=run[0], state=run[1])
            blockers = _overlap_blockers(candidate, refreshed, pruned_ids=pruned_ids)
            if blockers:
                raise ValueError(
                    "output path overlaps retained runs: " + ", ".join(blockers)
                )
            remote_result = prune_remote_output(
                ssh=str(candidate["ssh"]),
                python=str(candidate["python"]),
                output_path=str(candidate["source_path"]),
                output_root=candidate["output_root"],
                remote_workdir=str(candidate["remote_workdir"]),
                timeout=args.timeout,
            )
            record_source_output_deletion(
                execution_paths.registry_root,
                run_id,
                deletion_result=remote_result,
            )
            result["remote"] = remote_result
        except (OSError, OutputPruneOutcomeUnknown, RuntimeError, ValueError) as exc:
            result.update({"status": "failed", "error": str(exc)})
        finally:
            release_dispatch_lease(
                paths,
                server=str(candidate["server"]),
                machine_id=leased.machine_id,
                run_id=run_id,
                owner_token=leased.token,
            )
        results.append(result)
    return {
        "applied": True,
        "servers": sorted(requested_servers),
        "candidate_count": len(eligible),
        "pruned_count": sum(item["status"] == "pruned" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "already_pruned": already_pruned,
        "blocked": blocked,
        "results": results,
    }
