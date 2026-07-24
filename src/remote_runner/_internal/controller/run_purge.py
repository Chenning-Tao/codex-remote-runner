from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .. import output_sync
from ..execution_registry import (
    compact_run_events,
    project_paths,
    sha256_bytes,
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
    load_task_tombstone,
    replacement_dependent,
    run_purge_dir,
    run_purge_lock,
    stage_failed_queue_entry,
)


PLAN_SCHEMA = 1
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


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


def _replacement_policy(args: argparse.Namespace, run_id: str) -> dict[str, Any]:
    replacement = getattr(args, "replacement_run_id", None)
    no_replacement = getattr(args, "no_replacement", False) is True
    if (replacement is None) == (not no_replacement):
        raise ValueError(
            "run purge requires exactly one of --replacement-run-id or --no-replacement"
        )
    if replacement is not None:
        validated = validate_current_run_id(replacement)
        if validated == run_id:
            raise ValueError("a failed run cannot replace itself")
        return {"policy": "replacement", "replacement_run_id": validated}
    return {"policy": "explicit_none", "replacement_run_id": None}


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


def _source_provenance(source: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if kind == "queue":
        revision = source.get("revision")
    else:
        revision = source.get("source_revision") or source.get("expected_revision")
    command_digest = source.get("submitted_command_sha256")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError(f"{kind} record has no full source revision provenance")
    if (
        not isinstance(command_digest, str)
        or _SHA256_RE.fullmatch(command_digest) is None
    ):
        raise ValueError(f"{kind} record has no submitted-command provenance")
    output_metadata = source.get("output_metadata")
    result_tags = source.get("result_tags")
    if not isinstance(output_metadata, dict) or not isinstance(result_tags, dict):
        raise ValueError(f"{kind} record has invalid result provenance metadata")
    provenance = {
        "task_id": source.get("task_id"),
        "source_revision": revision,
        "submitted_command_sha256": command_digest,
        "workload_class": source.get("workload_class", "standard"),
        "result_intent": source.get("result_intent", "unclassified"),
        "result_tags": result_tags,
        "output_metadata": output_metadata,
    }
    for field in ("task_id", "workload_class", "result_intent"):
        if not isinstance(provenance[field], str) or not provenance[field]:
            raise ValueError(f"{kind} record has invalid {field} provenance")
    try:
        json.dumps(provenance, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{kind} record provenance is not canonical JSON") from exc
    return provenance


def _record_provenance(record: dict[str, Any]) -> tuple[dict[str, Any], str]:
    values: list[tuple[str, dict[str, Any]]] = []
    if record["job"] is not None:
        values.append(("queue", _source_provenance(record["job"], kind="queue")))
    if record["manifest"] is not None:
        values.append(
            ("execution", _source_provenance(record["manifest"], kind="execution"))
        )
    if not values:
        raise FileNotFoundError(
            f"run has no current controller record: {record['run_id']}"
        )
    provenance = values[0][1]
    for kind, candidate in values[1:]:
        if candidate != provenance:
            raise ValueError(
                f"queue and {kind} immutable provenance disagree for {record['run_id']}"
            )
    encoded = json.dumps(
        provenance,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return provenance, sha256_bytes(encoded)


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
    task = (
        manifest.get("task_id")
        if manifest is not None
        else None
        if job is None
        else job.get("task_id")
    )
    if isinstance(task, str) and load_task_tombstone(paths, task) is not None:
        blockers.append(
            {"run_id": run_id, "error": "the containing task is purging"}
        )
    dependent = replacement_dependent(paths, run_id)
    if dependent is not None:
        blockers.append(
            {
                "run_id": run_id,
                "error": "run is retained as replacement provenance",
                "dependent_run_id": dependent,
            }
        )
    return blockers


def _replacement_evidence(
    paths: Any,
    inventory: dict[str, Any],
    target_provenance: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, list[dict[str, Any]]]:
    if policy["policy"] == "explicit_none":
        return None, None, []
    replacement_run_id = str(policy["replacement_run_id"])
    blockers: list[dict[str, Any]] = []
    if load_run_tombstone(paths, replacement_run_id) is not None:
        blockers.append(
            {
                "run_id": replacement_run_id,
                "error": "replacement run is already purging or purged",
            }
        )
        return None, None, blockers
    replacement = _record_for_run(inventory, replacement_run_id)
    unsupported = _unsupported_rows(inventory, replacement_run_id)
    if unsupported:
        blockers.append(
            {
                "run_id": replacement_run_id,
                "error": "replacement run is not a current-schema execution",
            }
        )
        return None, None, blockers
    if replacement["manifest"] is None or replacement["run_state"] is None:
        blockers.append(
            {"run_id": replacement_run_id, "error": "replacement execution is missing"}
        )
        return None, None, blockers
    if replacement["run_state"]["status"] != "succeeded":
        blockers.append(
            {
                "run_id": replacement_run_id,
                "error": (
                    "replacement execution is "
                    f"{replacement['run_state']['status']}, not succeeded"
                ),
            }
        )
    try:
        replacement_provenance, replacement_digest = _record_provenance(replacement)
    except (FileNotFoundError, ValueError) as exc:
        blockers.append({"run_id": replacement_run_id, "error": str(exc)})
        return None, None, blockers
    if replacement_provenance != target_provenance:
        blockers.append(
            {
                "run_id": replacement_run_id,
                "error": "replacement immutable workload provenance does not match",
            }
        )
    return replacement, replacement_digest, blockers


def _inspect(
    paths: Any,
    run_id: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    inventory = load_purge_inventory(paths)
    record = _record_for_run(inventory, run_id)
    blockers = _target_blockers(paths, inventory, record)
    target_provenance: dict[str, Any] | None = None
    target_digest: str | None = None
    try:
        target_provenance, target_digest = _record_provenance(record)
    except (FileNotFoundError, ValueError) as exc:
        blockers.append({"run_id": run_id, "error": str(exc)})
    replacement = None
    replacement_digest = None
    if target_provenance is not None:
        replacement, replacement_digest, replacement_blockers = _replacement_evidence(
            paths,
            inventory,
            target_provenance,
            policy,
        )
        blockers.extend(replacement_blockers)
    blockers.extend(
        output_overlap_blockers(
            [record],
            inventory["all_jobs"],
            inventory["all_current"],
        )
    )
    try:
        sync_state = output_sync.inspect_failed_run_sync_state(
            paths.registry_root,
            run_id,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        sync_state = None
        blockers.append({"run_id": run_id, "error": str(exc)})
    return {
        "record": record,
        "replacement": replacement,
        "target_provenance": target_provenance,
        "target_provenance_sha256": target_digest,
        "replacement_provenance_sha256": replacement_digest,
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
    policy: dict[str, Any],
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
        "replacement_policy": policy["policy"],
        "replacement_run_id": policy["replacement_run_id"],
        "target_provenance": inspected["target_provenance"],
        "target_provenance_sha256": inspected["target_provenance_sha256"],
        "replacement_provenance_sha256": inspected["replacement_provenance_sha256"],
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
    policy: dict[str, Any],
) -> None:
    if plan["reason"] != reason:
        raise ValueError("run purge reason differs from the frozen plan")
    if plan["replacement_policy"] != policy["policy"] or plan.get(
        "replacement_run_id"
    ) != policy.get("replacement_run_id"):
        raise ValueError("run purge replacement policy differs from the frozen plan")


def _validate_tombstone_plan(tombstone: dict[str, Any], plan: dict[str, Any]) -> None:
    for field in (
        "run_id",
        "task_id",
        "reason",
        "replacement_policy",
        "replacement_run_id",
        "target_provenance_sha256",
        "replacement_provenance_sha256",
    ):
        if tombstone.get(field) != plan.get(field):
            raise ValueError(f"run purge tombstone and plan disagree on {field}")


def _validate_tombstone_request(
    tombstone: dict[str, Any],
    *,
    reason: str,
    policy: dict[str, Any],
) -> None:
    if tombstone["reason"] != reason:
        raise ValueError("run purge reason differs from the stored tombstone")
    if tombstone["replacement_policy"] != policy["policy"] or tombstone.get(
        "replacement_run_id"
    ) != policy.get("replacement_run_id"):
        raise ValueError(
            "run purge replacement policy differs from the stored tombstone"
        )


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


def _validate_frozen_replacement(
    paths: Any, plan: dict[str, Any]
) -> list[dict[str, Any]]:
    if plan["replacement_policy"] == "explicit_none":
        return []
    policy = {
        "policy": plan["replacement_policy"],
        "replacement_run_id": plan["replacement_run_id"],
    }
    inventory = load_purge_inventory(paths)
    _replacement, digest, blockers = _replacement_evidence(
        paths,
        inventory,
        plan["target_provenance"],
        policy,
    )
    if digest != plan["replacement_provenance_sha256"]:
        blockers.append(
            {
                "run_id": plan["replacement_run_id"],
                "error": "replacement provenance changed after the plan was frozen",
            }
        )
    return blockers


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


def _resource_summary(
    plan: dict[str, Any],
    preserved: list[dict[str, Any]],
) -> dict[str, Any]:
    run_id = str(plan["run_id"])
    artifact = plan["progress"]["run_artifacts"].get(run_id)
    return {
        "runtime_output": (
            "not_applicable" if artifact is None else artifact.get("status", "unknown")
        ),
        "output_sync_removed": (
            []
            if plan["progress"]["output_sync"] is None
            else plan["progress"]["output_sync"].get("removed_controller_state", [])
        ),
        "worktrees_removed": sum(
            1
            for value in plan["progress"]["worktrees"].values()
            if value.get("status") == "complete"
        ),
        "worktrees_preserved": len(preserved),
        "events": plan["progress"]["events"],
        "git_refs": "preserved",
    }


def _purge_run(args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_current_run_id(args.run_id)
    reason = _validate_reason(args.reason)
    policy = _replacement_policy(args, run_id)
    paths = controller_paths(args.controller_root, args.project_id)
    tombstone = load_run_tombstone(paths, run_id)
    plan = _load_plan(paths, run_id)
    if tombstone is not None:
        _validate_tombstone_request(tombstone, reason=reason, policy=policy)
    if tombstone is not None and tombstone["status"] == "purged":
        if args.apply:
            with contextlib.suppress(FileNotFoundError):
                shutil.rmtree(run_purge_dir(paths, run_id))
        return {
            "applied": bool(args.apply),
            "status": "already_purged",
            "run_id": run_id,
            "task_id": tombstone["task_id"],
        }

    if plan is not None:
        _validate_frozen_request(plan, reason=reason, policy=policy)
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
                "replacement": {
                    "policy": plan["replacement_policy"],
                    "run_id": plan["replacement_run_id"],
                    "target_provenance_sha256": plan["target_provenance_sha256"],
                    "replacement_provenance_sha256": plan[
                        "replacement_provenance_sha256"
                    ],
                },
            }
    else:
        try:
            inspected = _inspect(paths, run_id, policy)
        except FileNotFoundError:
            if tombstone is not None:
                return {
                    "applied": bool(args.apply),
                    "status": "attention_required",
                    "run_id": run_id,
                    "task_id": tombstone["task_id"],
                    "failures": [
                        {
                            "run_id": run_id,
                            "error": "purging tombstone exists without a resumable plan",
                        }
                    ],
                }
            raise
        public = _public_candidate(inspected["record"])
        replacement_public = {
            "policy": policy["policy"],
            "run_id": policy["replacement_run_id"],
            "target_provenance_sha256": inspected["target_provenance_sha256"],
            "replacement_provenance_sha256": inspected["replacement_provenance_sha256"],
            "matches": (
                policy["policy"] == "explicit_none"
                or (
                    inspected["replacement_provenance_sha256"] is not None
                    and inspected["replacement_provenance_sha256"]
                    == inspected["target_provenance_sha256"]
                )
            ),
        }
        if not args.apply:
            return {
                "applied": False,
                "status": "blocked" if inspected["blockers"] else "ready",
                "run_id": run_id,
                "task_id": (inspected["target_provenance"] or {}).get(
                    "task_id"
                ),
                "candidate": public,
                "replacement": replacement_public,
                "output_sync": inspected["output_sync"],
                "blockers": inspected["blockers"],
            }
        if inspected["blockers"]:
            return {
                "applied": True,
                "status": "blocked",
                "run_id": run_id,
                "candidate": public,
                "replacement": replacement_public,
                "blockers": inspected["blockers"],
            }
        target_digest = inspected["target_provenance_sha256"]
        assert isinstance(target_digest, str)
        record = inspected["record"]
        task = str((inspected["target_provenance"] or {})["task_id"])
        tombstone = create_run_tombstone(
            paths,
            run_id,
            task_id=task,
            reason=reason,
            replacement_policy=policy["policy"],
            replacement_run_id=policy["replacement_run_id"],
            target_provenance_sha256=target_digest,
            replacement_provenance_sha256=inspected["replacement_provenance_sha256"],
        )
        plan = _create_plan(
            paths,
            run_id,
            reason=reason,
            policy=policy,
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

    replacement_blockers = _validate_frozen_replacement(paths, plan)
    if replacement_blockers:
        return _attention(plan, replacement_blockers)

    latest = load_purge_inventory(paths)
    overlap = output_overlap_blockers(
        [plan["record"]],
        latest["all_jobs"],
        latest["all_current"],
    )
    if overlap:
        return _attention(plan, overlap)

    if plan["progress"]["output_sync"] is None:
        try:
            plan["progress"]["output_sync"] = output_sync.purge_failed_run_sync_state(
                paths.registry_root, run_id
            )
            _write_plan(paths, run_id, plan)
        except (OSError, RuntimeError, ValueError) as exc:
            return _attention(
                plan,
                [{"run_id": run_id, "error": str(exc)}],
            )

    failures = purge_execution_resources(
        paths,
        [plan["record"]],
        plan["progress"]["run_artifacts"],
        owner=f"purge-{run_id}",
        allowed_statuses=frozenset({"failed"}),
        timeout=args.timeout,
        remote_purge=purge_remote_run_artifacts,
        persist=lambda: _write_plan(paths, run_id, plan),
    )
    preserved: list[dict[str, Any]] = []
    if not failures:
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
    summary = _resource_summary(plan, preserved)
    try:
        complete_run_tombstone(paths, run_id, resource_summary=summary)
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
        "replacement": {
            "policy": plan["replacement_policy"],
            "run_id": plan["replacement_run_id"],
            "target_provenance_sha256": plan["target_provenance_sha256"],
            "replacement_provenance_sha256": plan["replacement_provenance_sha256"],
        },
        "preserved_resources": preserved,
        "git_refs": "preserved for separate source-cache garbage collection",
    }


def purge_run(args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_current_run_id(args.run_id)
    paths = controller_paths(args.controller_root, args.project_id)
    with run_purge_lock(paths, run_id):
        return _purge_run(args)
