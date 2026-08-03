from __future__ import annotations

import argparse
import contextlib
import shutil
from pathlib import Path
from typing import Any

from .. import output_sync
from ..execution_registry import (
    compact_run_events,
    project_paths,
    stage_failed_current_run,
    utc_now,
    validate_current_run_id,
    write_yaml,
)
from ..task_purge import purge_remote_run_artifacts, purge_remote_worktree
from .purge_common import (
    load_purge_inventory,
    output_overlap_blockers,
    public_records,
    purge_execution_resources,
    purge_worktrees,
)
from .registry import (
    complete_run_tombstone,
    controller_paths,
    create_run_tombstone,
    load_run_tombstone,
    run_purge_dir,
    run_purge_lock,
    stage_failed_queue_entry,
)


PLAN_SCHEMA = 1


def _validate_reason(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or len(value) > 512
    ):
        raise ValueError("run purge reason must be a single line of at most 512 chars")
    return value


def _plan_path(paths: Any, run_id: str) -> Path:
    return run_purge_dir(paths, run_id) / "plan.yaml"


def _load_plan(paths: Any, run_id: str) -> dict[str, Any] | None:
    path = _plan_path(paths, run_id)
    if not path.is_file():
        return None
    if path.is_symlink():
        raise ValueError(f"run purge plan must not be a symlink: {path}")
    from ..execution_registry import load_yaml

    plan = load_yaml(path)
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise ValueError(f"unsupported run purge plan schema: {path}")
    if plan.get("run_id") != run_id:
        raise ValueError(f"run purge plan identity mismatch: {path}")
    if not isinstance(plan.get("record"), dict) or not isinstance(
        plan.get("progress"), dict
    ):
        raise ValueError(f"invalid run purge plan: {path}")
    return plan


def _write_plan(paths: Any, run_id: str, plan: dict[str, Any]) -> None:
    write_yaml(_plan_path(paths, run_id), plan)


def _record_for_run(inventory: dict[str, Any], run_id: str) -> dict[str, Any]:
    queue = inventory["all_jobs"].get(run_id)
    execution = inventory["all_current"].get(run_id)
    return {
        "run_id": run_id,
        "job": None if queue is None else queue[0],
        "queue_state": None if queue is None else queue[1],
        "manifest": None if execution is None else execution[0],
        "run_state": None if execution is None else execution[1],
    }


def _unsupported_rows(inventory: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in inventory["rows"]
        if str(row.get("run_id")) == run_id and row.get("registry_kind") != "current"
    ]


def _target_blockers(
    paths: Any,
    inventory: dict[str, Any],
    record: dict[str, Any],
) -> list[dict[str, Any]]:
    run_id = str(record["run_id"])
    blockers: list[dict[str, Any]] = []
    unsupported = _unsupported_rows(inventory, run_id)
    for row in unsupported:
        blockers.append(
            {
                "run_id": run_id,
                "error": row.get("error") or "only current runs can be purged",
                "registry_kind": row.get("registry_kind"),
            }
        )
    job = record["job"]
    queue_state = record["queue_state"]
    manifest = record["manifest"]
    run_state = record["run_state"]
    if job is None and manifest is None and not unsupported:
        raise FileNotFoundError(f"controller run does not exist: {run_id}")
    if manifest is None:
        if queue_state is None or queue_state["status"] != "failed":
            status = None if queue_state is None else queue_state["status"]
            blockers.append(
                {"run_id": run_id, "error": f"queue-only run is {status}, not failed"}
            )
    else:
        if run_state is None or run_state["status"] != "failed":
            status = None if run_state is None else run_state["status"]
            blockers.append(
                {"run_id": run_id, "error": f"execution is {status}, not failed"}
            )
        if queue_state is not None and queue_state["status"] not in {
            "dispatched",
            "failed",
        }:
            blockers.append(
                {
                    "run_id": run_id,
                    "error": (
                        f"queue companion is {queue_state['status']}, not a failed "
                        "execution companion"
                    ),
                }
            )
    if job is not None and manifest is not None:
        if job["task_id"] != manifest["task_id"]:
            blockers.append(
                {
                    "run_id": run_id,
                    "error": "queue and execution task identity disagree",
                }
            )
    return blockers


def _inspect(
    paths: Any,
    run_id: str,
    *,
    delete_artifacts: bool,
) -> dict[str, Any]:
    inventory = load_purge_inventory(paths)
    record = _record_for_run(inventory, run_id)
    blockers = _target_blockers(paths, inventory, record)
    if delete_artifacts:
        blockers.extend(
            output_overlap_blockers(
                [record],
                inventory["all_jobs"],
                inventory["all_current"],
            )
        )
    sync_state: dict[str, Any] = {"status": "preserved"}
    if delete_artifacts:
        try:
            sync_state = output_sync.run_sync_status(
                paths.registry_root,
                run_id,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            blockers.append({"run_id": run_id, "error": str(exc)})
    return {
        "record": record,
        "inventory": inventory,
        "output_sync": sync_state,
        "blockers": blockers,
    }


def _public_candidate(record: dict[str, Any]) -> dict[str, Any]:
    return public_records([record])[0]


def _create_plan(
    paths: Any,
    run_id: str,
    *,
    reason: str,
    delete_artifacts: bool,
    inspected: dict[str, Any],
) -> dict[str, Any]:
    record = inspected["record"]
    task = (
        record["manifest"]["task_id"]
        if record["manifest"] is not None
        else record["job"]["task_id"]
    )
    plan = {
        "schema_version": PLAN_SCHEMA,
        "run_id": run_id,
        "task_id": task,
        "reason": reason,
        "delete_artifacts": delete_artifacts,
        "created_at": utc_now(),
        "record": record,
        "progress": {
            "staged_queue": False,
            "staged_execution": False,
            "output_sync": None,
            "run_artifacts": {},
            "worktrees": {},
            "events": None,
        },
    }
    _write_plan(paths, run_id, plan)
    return plan


def _validate_frozen_request(
    plan: dict[str, Any],
    *,
    reason: str,
    delete_artifacts: bool,
) -> None:
    if plan["reason"] != reason:
        raise ValueError("run purge reason differs from the frozen plan")
    if plan.get("delete_artifacts", False) is not delete_artifacts:
        raise ValueError("run purge artifact-deletion choice differs from the frozen plan")


def _validate_tombstone_plan(tombstone: dict[str, Any], plan: dict[str, Any]) -> None:
    if tombstone.get("run_id") != plan.get("run_id"):
        raise ValueError("run purge tombstone and plan disagree on run_id")


def _stage_record(paths: Any, plan: dict[str, Any]) -> None:
    progress = plan["progress"]
    record = plan["record"]
    run_id = str(plan["run_id"])
    task = str(plan["task_id"])
    if record["job"] is not None and not progress["staged_queue"]:
        stage_failed_queue_entry(paths, run_id, task_id=task)
        progress["staged_queue"] = True
        _write_plan(paths, run_id, plan)
    if record["manifest"] is not None and not progress["staged_execution"]:
        execution_paths = project_paths(paths.config_path)
        stage_failed_current_run(execution_paths, run_id, task_id=task)
        progress["staged_execution"] = True
        _write_plan(paths, run_id, plan)


def _attention(
    plan: dict[str, Any],
    failures: list[dict[str, Any]],
    *,
    preserved: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "applied": True,
        "status": "attention_required",
        "run_id": plan["run_id"],
        "task_id": plan["task_id"],
        "failures": failures,
        "preserved_resources": preserved or [],
    }


def _purge_run(args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_current_run_id(args.run_id)
    reason = _validate_reason(args.reason)
    delete_artifacts = bool(args.delete_artifacts)
    paths = controller_paths(args.controller_root, args.project_id)
    tombstone = load_run_tombstone(paths, run_id)
    plan = _load_plan(paths, run_id)
    if tombstone is not None and tombstone["status"] == "purged":
        if args.apply:
            with contextlib.suppress(FileNotFoundError):
                shutil.rmtree(run_purge_dir(paths, run_id))
        return {
            "applied": bool(args.apply),
            "status": "already_purged",
            "run_id": run_id,
        }

    if plan is not None:
        _validate_frozen_request(
            plan, reason=reason, delete_artifacts=delete_artifacts
        )
        if tombstone is None:
            raise ValueError("run purge plan exists without its tombstone")
        _validate_tombstone_plan(tombstone, plan)
        if not args.apply:
            return {
                "applied": False,
                "status": "attention_required",
                "run_id": run_id,
                "task_id": plan["task_id"],
                "candidate": _public_candidate(plan["record"]),
                "delete_artifacts": bool(plan.get("delete_artifacts", False)),
            }
    else:
        try:
            inspected = _inspect(paths, run_id, delete_artifacts=delete_artifacts)
        except FileNotFoundError:
            if tombstone is not None:
                return {
                    "applied": bool(args.apply),
                    "status": "attention_required",
                    "run_id": run_id,
                    "failures": [
                        {
                            "run_id": run_id,
                            "error": "purging tombstone exists without a resumable plan",
                        }
                    ],
                }
            raise
        public = _public_candidate(inspected["record"])
        record = inspected["record"]
        task = str(
            record["manifest"]["task_id"]
            if record["manifest"] is not None
            else record["job"]["task_id"]
        )
        if not args.apply:
            return {
                "applied": False,
                "status": "blocked" if inspected["blockers"] else "ready",
                "run_id": run_id,
                "task_id": task,
                "candidate": public,
                "output_sync": inspected["output_sync"],
                "delete_artifacts": delete_artifacts,
                "blockers": inspected["blockers"],
            }
        if inspected["blockers"]:
            return {
                "applied": True,
                "status": "blocked",
                "run_id": run_id,
                "candidate": public,
                "blockers": inspected["blockers"],
            }
        tombstone = create_run_tombstone(
            paths,
            run_id,
        )
        plan = _create_plan(
            paths,
            run_id,
            reason=reason,
            delete_artifacts=delete_artifacts,
            inspected={**inspected, "record": record},
        )
        _validate_tombstone_plan(tombstone, plan)

    assert plan is not None
    try:
        _stage_record(paths, plan)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        return _attention(
            plan,
            [{"run_id": run_id, "error": str(exc)}],
        )

    latest = load_purge_inventory(paths)
    overlap = (
        output_overlap_blockers(
            [plan["record"]],
            latest["all_jobs"],
            latest["all_current"],
        )
        if plan["delete_artifacts"]
        else []
    )
    if overlap:
        return _attention(plan, overlap)

    if plan["delete_artifacts"] and plan["progress"]["output_sync"] is None:
        try:
            record = plan["record"]
            sync_paths = output_sync.output_sync_paths(paths.registry_root)
            has_sync_state = any(
                (directory / f"{run_id}.json").is_file()
                for directory in (
                    sync_paths.pending_dir,
                    sync_paths.completed_dir,
                    sync_paths.state_dir,
                )
            )
            target_configs: dict[str, dict[str, Any]] = {}
            if has_sync_state and record["manifest"] is not None:
                stored_config = (
                    None if record["job"] is None else record["job"].get("output_sync")
                )
                if stored_config is None:
                    current_config = output_sync.load_config(paths.registry_root)
                    if current_config is None:
                        raise RuntimeError(
                            f"cannot locate output-sync target config for {run_id}"
                        )
                    stored_config = current_config.to_payload()
                target_configs[run_id] = stored_config
            plan["progress"]["output_sync"] = output_sync.purge_run_sync_state(
                paths.registry_root,
                {run_id},
                target_configs=target_configs,
                connect_timeout=args.timeout,
            )[0]
            _write_plan(paths, run_id, plan)
        except (OSError, RuntimeError, ValueError) as exc:
            return _attention(
                plan,
                [{"run_id": run_id, "error": str(exc)}],
            )

    failures = (
        purge_execution_resources(
            paths,
            [plan["record"]],
            plan["progress"]["run_artifacts"],
            owner=f"purge-{run_id}",
            allowed_statuses=frozenset({"failed"}),
            timeout=args.timeout,
            remote_purge=purge_remote_run_artifacts,
            persist=lambda: _write_plan(paths, run_id, plan),
        )
        if plan["delete_artifacts"]
        else []
    )
    preserved: list[dict[str, Any]] = []
    if plan["delete_artifacts"] and not failures:
        latest = load_purge_inventory(paths)
        worktree_failures, preserved = purge_worktrees(
            paths,
            [plan["record"]],
            plan["progress"]["worktrees"],
            latest,
            owner=f"purge-{run_id}",
            timeout=args.timeout,
            remote_purge=purge_remote_worktree,
            persist=lambda: _write_plan(paths, run_id, plan),
        )
        failures.extend(worktree_failures)
    if failures:
        return _attention(plan, failures, preserved=preserved)

    if plan["progress"]["events"] is None:
        try:
            plan["progress"]["events"] = (
                compact_run_events(project_paths(paths.config_path), {run_id})
                if paths.config_path.is_file()
                else {"removed": 0, "preserved": 0}
            )
            _write_plan(paths, run_id, plan)
        except (OSError, RuntimeError, ValueError) as exc:
            return _attention(
                plan,
                [{"run_id": run_id, "error": str(exc)}],
                preserved=preserved,
            )

    purge_root = run_purge_dir(paths, run_id)
    records_dir = purge_root / "records"
    if records_dir.is_symlink():
        return _attention(
            plan,
            [{"run_id": run_id, "error": "run purge staging path is a symlink"}],
            preserved=preserved,
        )
    try:
        shutil.rmtree(records_dir)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return _attention(
            plan,
            [{"run_id": run_id, "error": str(exc)}],
            preserved=preserved,
        )
    try:
        complete_run_tombstone(paths, run_id)
    except (OSError, RuntimeError, ValueError) as exc:
        return _attention(
            plan,
            [{"run_id": run_id, "error": str(exc)}],
            preserved=preserved,
        )
    with contextlib.suppress(FileNotFoundError, OSError):
        shutil.rmtree(run_purge_dir(paths, run_id))
    return {
        "applied": True,
        "status": "complete",
        "run_id": run_id,
        "task_id": plan["task_id"],
        "artifacts_deleted": bool(plan["delete_artifacts"]),
        "preserved_resources": preserved,
        "git_refs": "preserved for separate source-cache garbage collection",
    }


def purge_run(args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_current_run_id(args.run_id)
    paths = controller_paths(args.controller_root, args.project_id)
    with run_purge_lock(paths, run_id):
        return _purge_run(args)
