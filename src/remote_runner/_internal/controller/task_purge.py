from __future__ import annotations

import argparse
import contextlib
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from .. import output_sync
from ..execution_registry import (
    TERMINAL_STATUSES,
    compact_run_events,
    load_yaml,
    project_paths,
    stage_terminal_current_run,
    utc_now,
    write_yaml,
)
from ..stopping import stop as stop_execution
from ..task_purge import purge_remote_run_artifacts, purge_remote_worktree
from .purge_common import (
    load_purge_inventory,
    output_overlap_blockers,
    public_records,
    purge_execution_resources,
    purge_worktrees,
)
from .registry import (
    QUEUE_TERMINAL,
    complete_task_tombstone,
    controller_paths,
    create_task_tombstone,
    load_task_tombstone,
    stage_terminal_queue_entry,
    task_identity_digest,
    task_purge_dir,
    transition_queued_state,
    validate_task_identity,
)


PLAN_SCHEMA = 1


def _current_inventory(paths: Any, task_id: str) -> dict[str, Any]:
    inventory = load_purge_inventory(paths)
    jobs_by_id = inventory["all_jobs"]
    current = inventory["all_current"]
    rows = inventory["rows"]
    unsupported: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row.get("run_id"))
        if row.get("registry_kind") != "current":
            if row.get("task_id") == task_id:
                unsupported.append(
                    {
                        "run_id": run_id,
                        "registry_kind": row.get("registry_kind"),
                        "error": row.get("error") or "only current runs can be purged",
                    }
                )
            continue
    selected_ids = {
        run_id
        for run_id, (job, _state) in jobs_by_id.items()
        if job["task_id"] == task_id
    }
    selected_ids.update(
        run_id
        for run_id, (manifest, _state) in current.items()
        if manifest["task_id"] == task_id
    )
    blockers = list(unsupported)
    records: list[dict[str, Any]] = []
    for run_id in sorted(selected_ids):
        queue = jobs_by_id.get(run_id)
        execution = current.get(run_id)
        if queue is not None and queue[0]["task_id"] != task_id:
            blockers.append(
                {"run_id": run_id, "error": "queue record belongs to another task"}
            )
        if execution is not None and execution[0]["task_id"] != task_id:
            blockers.append(
                {"run_id": run_id, "error": "execution record belongs to another task"}
            )
        if (
            queue is not None
            and queue[1]["status"] == "dispatched"
            and execution is None
        ):
            blockers.append(
                {
                    "run_id": run_id,
                    "error": "dispatched queue record has no execution authority",
                }
            )
        records.append(
            {
                "run_id": run_id,
                "job": None if queue is None else queue[0],
                "queue_state": None if queue is None else queue[1],
                "manifest": None if execution is None else execution[0],
                "run_state": None if execution is None else execution[1],
            }
        )

    blockers.extend(output_overlap_blockers(records, jobs_by_id, current))

    identities = sorted(
        {str(job["task_id"]) for job, _state in jobs_by_id.values()}
        | {str(manifest["task_id"]) for manifest, _state in current.values()}
        | {
            str(row["task_id"])
            for row in rows
            if isinstance(row.get("task_id"), str)
        }
    )
    return {
        "records": records,
        "blockers": blockers,
        "all_jobs": jobs_by_id,
        "all_current": current,
        "known_task_identities": identities,
        "execution_paths": inventory["execution_paths"],
    }


def _plan_path(paths: Any, task_id: str) -> Path:
    return task_purge_dir(paths, task_id) / "plan.yaml"


def _load_plan(paths: Any, task_id: str) -> dict[str, Any] | None:
    path = _plan_path(paths, task_id)
    if not path.is_file():
        return None
    plan = load_yaml(path)
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise ValueError(f"unsupported task purge plan schema: {path}")
    if plan.get("task_id") != task_id:
        raise ValueError(f"task purge plan identity mismatch: {path}")
    records = plan.get("records")
    progress = plan.get("progress")
    if not isinstance(records, list) or not isinstance(progress, dict):
        raise ValueError(f"invalid task purge plan: {path}")
    return plan


def _write_plan(paths: Any, task_id: str, plan: dict[str, Any]) -> None:
    write_yaml(_plan_path(paths, task_id), plan)


def _stop_task_records(
    paths: Any,
    records: list[dict[str, Any]],
    *,
    timeout: int,
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    execution_paths = (
        project_paths(paths.config_path) if paths.config_path.is_file() else None
    )
    for record in records:
        run_id = str(record["run_id"])
        queue_state = record["queue_state"]
        if queue_state is not None:
            queue_status = str(queue_state["status"])
            if queue_status == "queued":
                try:
                    transition_queued_state(
                        paths,
                        run_id,
                        expected_revision=int(queue_state["revision"]),
                        status="stopped",
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    failures.append({"run_id": run_id, "error": str(exc)})
                    continue
            elif queue_status == "dispatching":
                failures.append(
                    {
                        "run_id": run_id,
                        "error": "run is currently dispatching; retry after dispatch resolves",
                    }
                )
                continue
        run_state = record["run_state"]
        if (
            run_state is not None
            and run_state["status"] not in TERMINAL_STATUSES
            and execution_paths is not None
        ):
            try:
                stop_execution(execution_paths, run_id, timeout)
            except (OSError, RuntimeError, ValueError) as exc:
                failures.append({"run_id": run_id, "error": str(exc)})
    return failures


def _create_plan(
    paths: Any,
    task_id: str,
    reason: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    plan = {
        "schema_version": PLAN_SCHEMA,
        "task_id": task_id,
        "task_digest": task_identity_digest(task_id),
        "reason": reason,
        "created_at": utc_now(),
        "records": records,
        "progress": {
            "output_sync": None,
            "run_artifacts": {},
            "worktrees": {},
            "staged_queue": [],
            "staged_runs": [],
            "events": None,
        },
    }
    _write_plan(paths, task_id, plan)
    return plan


def _purge_output_sync(
    paths: Any,
    plan: dict[str, Any],
    *,
    timeout: int,
) -> None:
    progress = plan["progress"]
    if progress["output_sync"] is not None:
        return
    run_ids = {str(record["run_id"]) for record in plan["records"]}
    sync_paths = output_sync.output_sync_paths(paths.registry_root)
    current_config = output_sync.load_config(paths.registry_root)
    target_configs: dict[str, dict[str, Any]] = {}
    for record in plan["records"]:
        run_id = str(record["run_id"])
        should_purge_target = (
            record["manifest"] is not None
            and record["manifest"].get("output_path") is not None
            and record["run_state"] is not None
            and record["run_state"]["status"] == "succeeded"
            and (
                (sync_paths.pending_dir / f"{run_id}.json").is_file()
                or (sync_paths.completed_dir / f"{run_id}.json").is_file()
                or (
                    record["job"] is not None
                    and record["job"].get("output_sync") is not None
                )
            )
        )
        if not should_purge_target:
            continue
        stored_config = (
            None if record["job"] is None else record["job"].get("output_sync")
        )
        if stored_config is None:
            if current_config is None:
                raise RuntimeError(
                    f"cannot locate output-sync target config for {run_id}"
                )
            stored_config = current_config.to_payload()
        target_configs[run_id] = stored_config
    progress["output_sync"] = output_sync.purge_run_sync_state(
        paths.registry_root,
        run_ids,
        target_configs=target_configs,
        connect_timeout=timeout,
    )
    _write_plan(paths, str(plan["task_id"]), plan)


def _purge_run_resources(
    paths: Any,
    plan: dict[str, Any],
    *,
    timeout: int,
) -> list[dict[str, str]]:
    owner = f"purge-{str(plan['task_digest'])[:16]}"
    return purge_execution_resources(
        paths,
        plan["records"],
        plan["progress"]["run_artifacts"],
        owner=owner,
        allowed_statuses=frozenset(TERMINAL_STATUSES),
        timeout=timeout,
        remote_purge=purge_remote_run_artifacts,
        persist=lambda: _write_plan(paths, str(plan["task_id"]), plan),
    )


def _purge_worktrees(
    paths: Any,
    plan: dict[str, Any],
    inventory: dict[str, Any],
    *,
    timeout: int,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    owner = f"purge-{str(plan['task_digest'])[:16]}"
    return purge_worktrees(
        paths,
        plan["records"],
        plan["progress"]["worktrees"],
        inventory,
        owner=owner,
        timeout=timeout,
        remote_purge=purge_remote_worktree,
        persist=lambda: _write_plan(paths, str(plan["task_id"]), plan),
    )


def _stage_records(paths: Any, plan: dict[str, Any]) -> None:
    task_id = str(plan["task_id"])
    purge_root = task_purge_dir(paths, task_id)
    progress = plan["progress"]
    staged_queue = set(progress["staged_queue"])
    staged_runs = set(progress["staged_runs"])
    execution_paths = (
        project_paths(paths.config_path) if paths.config_path.is_file() else None
    )
    for record in plan["records"]:
        run_id = str(record["run_id"])
        if record["job"] is not None and run_id not in staged_queue:
            stage_terminal_queue_entry(
                paths,
                run_id,
                task_id=task_id,
            )
            progress["staged_queue"].append(run_id)
            staged_queue.add(run_id)
            _write_plan(paths, task_id, plan)
        if record["manifest"] is not None and run_id not in staged_runs:
            if execution_paths is None:
                raise RuntimeError("controller project config is unavailable")
            stage_terminal_current_run(
                execution_paths,
                run_id,
                task_id=task_id,
                destination=purge_root / "records" / run_id / "execution",
            )
            progress["staged_runs"].append(run_id)
            staged_runs.add(run_id)
            _write_plan(paths, task_id, plan)


def purge_task(args: argparse.Namespace) -> dict[str, Any]:
    task_id = validate_task_identity(args.task_id)
    if (
        not isinstance(args.reason, str)
        or not args.reason.strip()
        or "\x00" in args.reason
        or "\n" in args.reason
        or "\r" in args.reason
        or len(args.reason) > 512
    ):
        raise ValueError("task purge reason must be a single line of at most 512 chars")
    paths = controller_paths(args.controller_root, args.project_id)
    existing_plan = _load_plan(paths, task_id)
    tombstone = load_task_tombstone(paths, task_id)
    if tombstone is not None and tombstone.get("status") == "purged":
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(task_purge_dir(paths, task_id))
        return {
            "applied": bool(args.apply),
            "status": "already_purged",
            "task_id": task_id,
            "candidate_count": 0,
        }

    inventory = _current_inventory(paths, task_id)
    records = inventory["records"]
    if existing_plan is not None:
        records = existing_plan["records"]
    if not records:
        if inventory["blockers"]:
            return {
                "applied": bool(args.apply),
                "status": "blocked",
                "task_id": task_id,
                "candidate_count": 0,
                "blockers": inventory["blockers"],
            }
        basename_matches = [
            identity
            for identity in inventory["known_task_identities"]
            if PurePosixPath(identity).name == PurePosixPath(task_id).name
        ]
        if basename_matches:
            raise ValueError(
                "task purge requires the exact stored task identity; matches: "
                + ", ".join(basename_matches)
            )
        raise FileNotFoundError(
            f"task has no controller records: {task_id}"
        )

    public = public_records(records)
    if not args.apply:
        return {
            "applied": False,
            "status": "blocked" if inventory["blockers"] else "ready",
            "task_id": task_id,
            "candidate_count": len(records),
            "candidates": public,
            "blockers": inventory["blockers"],
        }
    if inventory["blockers"]:
        return {
            "applied": True,
            "status": "blocked",
            "task_id": task_id,
            "candidate_count": len(records),
            "blockers": inventory["blockers"],
        }

    create_task_tombstone(paths, task_id, reason=args.reason)
    if existing_plan is None:
        stop_failures = _stop_task_records(paths, records, timeout=args.timeout)
        refreshed = _current_inventory(paths, task_id)
        if refreshed["blockers"]:
            return {
                "applied": True,
                "status": "blocked",
                "task_id": task_id,
                "candidate_count": len(records),
                "blockers": refreshed["blockers"],
            }
        if stop_failures or any(
            record["queue_state"] is not None
            and record["queue_state"]["status"] not in QUEUE_TERMINAL
            or record["run_state"] is not None
            and record["run_state"]["status"] not in TERMINAL_STATUSES
            for record in refreshed["records"]
        ):
            return {
                "applied": True,
                "status": "waiting_for_terminal",
                "task_id": task_id,
                "candidate_count": len(records),
                "failures": stop_failures,
            }
        records = refreshed["records"]
        existing_plan = _create_plan(
            paths,
            task_id,
            args.reason,
            records,
        )
        inventory = refreshed

    plan = existing_plan
    assert plan is not None
    failures: list[dict[str, str]] = []
    preserved: list[dict[str, Any]] = []
    latest_inventory = _current_inventory(paths, task_id)
    output_blockers = output_overlap_blockers(
        plan["records"],
        latest_inventory["all_jobs"],
        latest_inventory["all_current"],
    )
    if output_blockers:
        return {
            "applied": True,
            "status": "blocked",
            "task_id": task_id,
            "candidate_count": len(records),
            "blockers": output_blockers,
        }
    try:
        _stage_records(paths, plan)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        return {
            "applied": True,
            "status": "attention_required",
            "task_id": task_id,
            "candidate_count": len(records),
            "failures": [{"run_id": "controller-registry", "error": str(exc)}],
        }
    try:
        _purge_output_sync(paths, plan, timeout=args.timeout)
    except (OSError, RuntimeError, ValueError) as exc:
        failures.append({"run_id": "output-sync", "error": str(exc)})
    if not failures:
        failures.extend(_purge_run_resources(paths, plan, timeout=args.timeout))
    if not failures:
        worktree_failures, preserved = _purge_worktrees(
            paths,
            plan,
            latest_inventory,
            timeout=args.timeout,
        )
        failures.extend(worktree_failures)
    if failures:
        return {
            "applied": True,
            "status": "attention_required",
            "task_id": task_id,
            "candidate_count": len(records),
            "failures": failures,
            "preserved_resources": preserved,
        }

    execution_paths = project_paths(paths.config_path)
    if plan["progress"]["events"] is None:
        plan["progress"]["events"] = compact_run_events(
            execution_paths,
            {str(record["run_id"]) for record in records},
        )
        _write_plan(paths, task_id, plan)

    purge_root = task_purge_dir(paths, task_id)
    records_dir = purge_root / "records"
    if records_dir.is_symlink():
        raise ValueError("task purge staging records path is a symlink")
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(records_dir)
    complete_task_tombstone(paths, task_id)
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(purge_root)
    return {
        "applied": True,
        "status": "complete",
        "task_id": task_id,
        "purged_count": len(records),
        "run_ids": sorted(str(record["run_id"]) for record in records),
        "preserved_resources": preserved,
        "git_refs": "preserved for separate source-cache garbage collection",
    }
