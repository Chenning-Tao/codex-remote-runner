from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

from .. import monitoring
from ..cleanup import CleanupOutcomeUnknown, cleanup_remote_runtime
from ..decommissioned_run import inspect_or_close, validate_request
from ..execution_registry import (
    load_current_run,
    project_paths,
    purge_current_run,
    registry_kind,
    utc_now,
    validate_current_run_id,
)
from ..output_sync import (
    disable_config,
    list_pending,
    run_sync_status,
    store_config,
    sync_status,
)
from ..run_readiness import cohort_report_readiness
from ..scheduling import normalize_workload_class
from ..stopping import stop as stop_execution
from ..tmux import dispatcher_tmux_session, exact_tmux_target, resolve_tmux_executable
from .dashboard import collect_server_snapshot, enrich_active_runs, validate_payload
from .output_prune import prune_outputs
from .run_purge import purge_run
from .task_purge import purge_task
from .registry import (
    ControllerPaths,
    QUEUE_TERMINAL,
    controller_paths,
    ensure_server_capacities,
    eligible_prepared_servers,
    list_drained_servers,
    list_jobs,
    list_queued,
    list_queued_all,
    load_job,
    placement_update_active,
    purge_queue_entry,
    release_queued_job_update,
    reserve_queued_job_update,
    set_server_drained,
    submit_job,
    transition_queued_state,
    update_queued_job,
    update_server_capacity,
    extend_queued_all,
    extend_queued_job,
)
from .output_sync_worker import ensure_output_sync_worker
from .run_view import ACTIVE_PHASES, load_run_view


OVERVIEW_RECORD_LIMIT = 20
MAX_WAIT_SECONDS = 55
MAX_WAIT_RUNS = 64
ETAG_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def ensure_dispatcher(
    *,
    controller_root: Path,
    project_id: str,
    timeout: int,
    interval: int,
) -> bool:
    tmux = resolve_tmux_executable()
    session = dispatcher_tmux_session(project_id)
    target = exact_tmux_target(session)
    exists = subprocess.run(
        [tmux, "has-session", "-t", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if exists.returncode == 0:
        return False
    started = subprocess.run(
        [
            tmux,
            "new-session",
            "-d",
            "-s",
            session,
            sys.executable,
            "-m",
            "remote_runner._internal.controller.dispatcher",
            "--controller-root",
            str(controller_root),
            "--project-id",
            project_id,
            "--timeout",
            str(timeout),
            "--interval",
            str(interval),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        raced = subprocess.run(
            [tmux, "has-session", "-t", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if raced.returncode == 0:
            return False
        raise RuntimeError(
            started.stderr.strip() or "failed to start controller dispatcher"
        )
    return True


def _read_object(noun: str) -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdin must contain one JSON {noun} object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"stdin must contain one JSON {noun} object")
    return value


def _read_optional_object(noun: str) -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdin must contain one JSON {noun} object: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"stdin must contain one JSON {noun} object")
    return value


def submit(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    job = _read_object("job")
    prepared_servers = job.get("prepared_servers")
    if not isinstance(prepared_servers, list) or any(
        not isinstance(server, dict) for server in prepared_servers
    ):
        raise ValueError("job prepared_servers must be a list of mappings")
    ensure_server_capacities(paths, prepared_servers)
    if "output_sync" in job:
        output_sync = job.get("output_sync")
        if output_sync is None:
            disable_config(paths.registry_root)
        else:
            store_config(paths.registry_root, output_sync)
    entry = submit_job(paths, job)
    dispatcher_started = ensure_dispatcher(
        controller_root=args.controller_root,
        project_id=args.project_id,
        timeout=args.timeout,
        interval=args.interval,
    )
    return {
        "queue_entry": str(entry),
        "outcome": {"action": "submitted", "run_id": entry.name},
        "dispatcher_started": dispatcher_started,
    }


def pending_all(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    jobs = list_queued_all(paths)
    return {"jobs": jobs, "count": len(jobs)}


def extend_all(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    payload = _read_object("pool update")
    updates = payload.get("updates")
    if not isinstance(updates, list):
        raise ValueError("pool update requires an updates list")
    for update in updates:
        if not isinstance(update, dict) or not isinstance(
            update.get("prepared_servers"), list
        ):
            raise ValueError("pool update prepared_servers must be a list")
        additions = update["prepared_servers"]
        if any(not isinstance(server, dict) for server in additions):
            raise ValueError("pool update prepared server must be a mapping")
        if additions:
            ensure_server_capacities(paths, additions)
    results = extend_queued_all(paths, updates)
    dispatcher_started = ensure_dispatcher(
        controller_root=args.controller_root,
        project_id=args.project_id,
        timeout=args.timeout,
        interval=args.interval,
    )
    return {
        "results": results,
        "extended_count": sum(item["status"] == "extended" for item in results),
        "dispatcher_started": dispatcher_started,
    }


def queued_job(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    run_id = validate_current_run_id(args.run_id)
    job, state = load_job(paths, run_id)
    if state["status"] != "queued":
        raise ValueError(f"queued run {run_id} is {state['status']}, not queued")
    return {
        "job": {
            "run_id": job["run_id"],
            "revision": job["revision"],
            "minimum_cores": job["minimum_cores"],
            "requested_cores": job.get("requested_cores"),
            "workload_class": job["workload_class"],
            "prepared_servers": [
                str(server["name"]) for server in job["prepared_servers"]
            ],
            "output_relpath": job["output_relpath"],
            "output_path": job["output_path"],
        }
    }


def extend_job(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    payload = _read_object("queued job extension")
    revision = payload.get("revision")
    prepared_servers = payload.get("prepared_servers")
    placement_token = payload.get("placement_token")
    if not isinstance(revision, str) or not isinstance(prepared_servers, list):
        raise ValueError("queued job extension requires revision and prepared_servers")
    if placement_token is not None and not isinstance(placement_token, str):
        raise ValueError("queued job extension placement_token must be a string")
    if any(not isinstance(server, dict) for server in prepared_servers):
        raise ValueError("queued job extension prepared server must be a mapping")
    ensure_server_capacities(paths, prepared_servers)
    result = extend_queued_job(
        paths,
        args.run_id,
        revision=revision,
        prepared_servers=prepared_servers,
        placement_token=placement_token,
    )
    dispatcher_started = ensure_dispatcher(
        controller_root=args.controller_root,
        project_id=args.project_id,
        timeout=args.timeout,
        interval=args.interval,
    )
    return {**result, "dispatcher_started": dispatcher_started}


def edit_queued_job(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    payload = _read_object("queued job update")
    allowed = {
        "expected_revision",
        "queue_priority",
        "workload_class",
        "eligible_servers",
        "move",
        "placement_token",
    }
    unexpected = set(payload) - allowed
    if unexpected:
        raise ValueError(
            "queued job update contains unexpected fields: "
            + ", ".join(sorted(unexpected))
        )
    expected_revision = payload.get("expected_revision")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise ValueError("queued job update requires an integer expected_revision")
    queue_priority = payload.get("queue_priority")
    if queue_priority is not None and not isinstance(queue_priority, str):
        raise ValueError("queued job queue_priority must be a string")
    workload_class = payload.get("workload_class")
    if workload_class is not None and not isinstance(workload_class, str):
        raise ValueError("queued job workload_class must be a string")
    eligible_servers = payload.get("eligible_servers")
    if eligible_servers is not None and not isinstance(eligible_servers, list):
        raise ValueError("queued job eligible_servers must be a list")
    move = payload.get("move")
    if move is not None and not isinstance(move, str):
        raise ValueError("queued job move must be a string")
    placement_token = payload.get("placement_token")
    if placement_token is not None and not isinstance(placement_token, str):
        raise ValueError("queued job placement_token must be a string")
    if workload_class is not None or eligible_servers is not None:
        current_job, _current_state = load_job(paths, args.run_id)
        capacities = ensure_server_capacities(paths, current_job["prepared_servers"])
        target_class = normalize_workload_class(
            workload_class or current_job["workload_class"]
        )
        selected_servers = eligible_servers or list(current_job["eligible_servers"])
        prepared_by_name = {
            str(server["name"]): server for server in current_job["prepared_servers"]
        }
        slot_field = f"{target_class}_slots"
        if not any(
            isinstance(name, str)
            and name in prepared_by_name
            and int(
                capacities.get(
                    str(prepared_by_name[name].get("machine_id", name)), {}
                ).get(slot_field, 0)
            )
            > 0
            for name in selected_servers
        ):
            raise ValueError(
                f"queued job requires an eligible server with positive {slot_field}"
            )
    result = update_queued_job(
        paths,
        args.run_id,
        expected_revision=expected_revision,
        queue_priority=queue_priority,
        workload_class=workload_class,
        eligible_servers=eligible_servers,
        move=move,
        placement_token=placement_token,
    )
    dispatcher_started = False
    if result["changed"]:
        dispatcher_started = ensure_dispatcher(
            controller_root=args.controller_root,
            project_id=args.project_id,
            timeout=args.timeout,
            interval=args.interval,
        )
    return {
        "changed": result["changed"],
        "job": _compact_queue_item(result["job"], result["state"])["job"],
        "state": result["state"],
        "dispatcher_started": dispatcher_started,
    }


def reserve_queue_update(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    payload = _read_object("queue update reservation")
    if set(payload) != {"expected_revision", "requested_servers", "ttl_seconds"}:
        raise ValueError("queue update reservation payload is invalid")
    result = reserve_queued_job_update(
        paths,
        args.run_id,
        expected_revision=payload["expected_revision"],
        requested_servers=payload["requested_servers"],
        ttl_seconds=payload["ttl_seconds"],
    )
    return {
        "token": result["token"],
        "state": _compact_queue_item(load_job(paths, args.run_id)[0], result["state"])[
            "state"
        ],
    }


def release_queue_update(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    payload = _read_object("queue update release")
    if set(payload) != {"token"} or not isinstance(payload.get("token"), str):
        raise ValueError("queue update release payload is invalid")
    result = release_queued_job_update(paths, args.run_id, token=payload["token"])
    dispatcher_started = ensure_dispatcher(
        controller_root=args.controller_root,
        project_id=args.project_id,
        timeout=args.timeout,
        interval=args.interval,
    )
    return {**result, "dispatcher_started": dispatcher_started}


def _task_key(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    key = PurePosixPath(value).name
    if key in {"", ".", ".."}:
        return None
    return key


def _task_selector(value: str | None) -> str | None:
    if value is None:
        return None
    key = _task_key(value)
    if key is None:
        raise ValueError("--task-id must identify one task")
    return key


def _status_counts(values: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _execution_status(row: dict[str, Any]) -> str:
    for field in ("authoritative_status", "stored_status"):
        value = row.get(field)
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _active_execution(row: dict[str, Any]) -> bool:
    return _execution_status(row) in {"registered", "running"}


def _compact_queue_item(
    job: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    prepared = job.get("prepared_servers")
    eligible_servers = []
    if isinstance(prepared, list):
        eligible_servers = [
            str(item["name"])
            for item in prepared
            if isinstance(item, dict) and item.get("name") is not None
        ]
    compact_job = {
        field: job[field]
        for field in (
            "run_id",
            "label",
            "task_id",
            "workload_class",
            "queue_priority",
            "queue_position",
            "minimum_cores",
            "requested_cores",
            "server_scope",
            "created_at",
        )
        if field in job
    }
    compact_job["supported_servers"] = eligible_servers
    selected = job.get("eligible_servers")
    compact_job["eligible_servers"] = (
        [str(name) for name in selected]
        if isinstance(selected, list)
        else eligible_servers
    )
    compact_job["portable_output"] = job.get("output_path") is None
    compact_job["requires_output_root"] = job.get("output_relpath") is not None
    compact_state = {
        field: state[field]
        for field in (
            "status",
            "revision",
            "created_at",
            "updated_at",
            "error",
        )
        if field in state
    }
    placement_update = state.get("placement_update")
    if isinstance(placement_update, dict) and placement_update_active(state):
        compact_state["placement_update"] = {
            "status": "preparing",
            "expires_at": placement_update.get("expires_at"),
            "requested_servers": placement_update.get("requested_servers"),
        }
    return {"job": compact_job, "state": compact_state}


def _compact_execution(row: dict[str, Any]) -> dict[str, Any]:
    return {
        field: row[field]
        for field in (
            "run_id",
            "label",
            "task_id",
            "server",
            "workload_class",
            "authoritative_status",
            "observation",
            "observation_source",
            "progress",
            "error",
            "failure",
            "created_at",
            "started_at",
            "updated_at",
            "finished_at",
            "exit_code",
            "privacy_mode",
        )
        if field in row
    }


def _status_summary(
    jobs: list[tuple[dict[str, Any], dict[str, Any]]],
    rows: list[dict[str, Any]],
    *,
    queue_matched: int,
    queue_returned: int,
    runs_matched: int,
    runs_returned: int,
) -> dict[str, Any]:
    queue_statuses = [str(state["status"]) for _job, state in jobs]
    run_statuses = [_execution_status(row) for row in rows]
    return {
        "queue": {
            "total": len(jobs),
            "active": sum(
                status in {"queued", "dispatching"} for status in queue_statuses
            ),
            "matched": queue_matched,
            "returned": queue_returned,
            "omitted": max(0, queue_matched - queue_returned),
            "by_status": _status_counts(queue_statuses),
        },
        "runs": {
            "total": len(rows),
            "active": sum(_active_execution(row) for row in rows),
            "matched": runs_matched,
            "returned": runs_returned,
            "omitted": max(0, runs_matched - runs_returned),
            "by_authoritative_status": _status_counts(run_statuses),
        },
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    task_selector = _task_selector(getattr(args, "task_id", None))
    overview = args.run_id is None and task_selector is None
    all_jobs = (
        []
        if args.run_id is not None
        else list_jobs(paths, statuses={"queued", "dispatching"})
        if overview
        else list_jobs(paths)
    )
    if args.run_id is not None:
        try:
            job, state = load_job(paths, args.run_id)
        except FileNotFoundError:
            queue = []
        else:
            queue = [{"job": job, "state": state}]
    elif task_selector is not None:
        queue = [
            {"job": job, "state": state}
            for job, state in all_jobs
            if _task_key(job.get("task_id")) == task_selector
        ]
    else:
        dispatching = [row for row in all_jobs if row[1]["status"] == "dispatching"]
        queue = [
            {"job": job, "state": state}
            for job, state in (dispatching + list_queued(paths, jobs=all_jobs))
        ]
    runs: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    if paths.config_path.is_file():
        execution_paths = project_paths(paths.config_path)
        all_rows = monitoring.load_registry_rows(
            execution_paths,
            only_run_id=args.run_id,
            active_only=overview,
        )
        rows = all_rows
        if task_selector is not None:
            rows = [
                row for row in rows if _task_key(row.get("task_id")) == task_selector
            ]
        elif overview:
            rows = [row for row in rows if _active_execution(row)]
        runs = monitoring.monitor_rows(
            execution_paths,
            rows,
            args.timeout,
            no_write=False,
            isolate_errors=True,
        )
    if any(
        item["state"]["status"] in {"queued", "dispatching"} for item in queue
    ) or any(
        row.get("registry_kind") == "current"
        and row.get("authoritative_status") not in {"succeeded", "failed", "stopped"}
        for row in runs
    ):
        ensure_dispatcher(
            controller_root=args.controller_root,
            project_id=args.project_id,
            timeout=args.timeout,
            interval=args.interval,
        )
    output_sync_worker_error = None
    try:
        output_sync_worker_started = ensure_output_sync_worker(
            paths,
            timeout=args.timeout,
            interval=args.interval,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        output_sync_worker_started = False
        output_sync_worker_error = str(exc)
    full_overview = bool(getattr(args, "_full_overview", False))
    returned_queue = (
        queue[:OVERVIEW_RECORD_LIMIT] if overview and not full_overview else queue
    )
    returned_runs = (
        runs[:OVERVIEW_RECORD_LIMIT] if overview and not full_overview else runs
    )
    result: dict[str, Any] = {
        "queue": (
            [_compact_queue_item(item["job"], item["state"]) for item in returned_queue]
            if overview
            else returned_queue
        ),
        "runs": (
            [_compact_execution(row) for row in returned_runs]
            if overview
            else returned_runs
        ),
    }
    if overview:
        monitored_by_id = {str(row.get("run_id")): row for row in runs}
        summarized_rows = [
            monitored_by_id.get(str(row.get("run_id")), row) for row in all_rows
        ]
        result["summary"] = _status_summary(
            all_jobs,
            summarized_rows,
            queue_matched=len(queue),
            queue_returned=len(returned_queue),
            runs_matched=len(runs),
            runs_returned=len(returned_runs),
        )
        result["output_sync"] = {
            **sync_status(paths.registry_root),
            "worker_started": output_sync_worker_started,
            "worker_error": output_sync_worker_error,
        }
        result["server_drains"] = {
            "scope": "controller",
            "servers": list_drained_servers(paths),
        }
    elif args.run_id is not None:
        result["output_sync"] = run_sync_status(paths.registry_root, args.run_id)
        result["run_view"] = load_run_view(paths, args.run_id)
    elif task_selector is not None:
        task_jobs = [(item["job"], item["state"]) for item in queue]
        result["summary"] = _status_summary(
            task_jobs,
            runs,
            queue_matched=len(queue),
            queue_returned=len(returned_queue),
            runs_matched=len(runs),
            runs_returned=len(returned_runs),
        )
    return result


def wait_run(args: argparse.Namespace) -> dict[str, Any]:
    wait_seconds = args.wait_seconds
    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, int)
        or not 0 <= wait_seconds <= MAX_WAIT_SECONDS
    ):
        raise ValueError(f"--wait-seconds must be between 0 and {MAX_WAIT_SECONDS}")
    after_etag = args.after_etag
    if after_etag is not None and ETAG_RE.fullmatch(after_etag) is None:
        raise ValueError("--after-etag must be a remote-runner run-view etag")

    view = load_run_view(
        controller_paths(args.controller_root, args.project_id),
        args.run_id,
    )
    dispatcher_started = False
    if view["phase"] in ACTIVE_PHASES or view["phase"] == "attention_required":
        dispatcher_started = ensure_dispatcher(
            controller_root=args.controller_root,
            project_id=args.project_id,
            timeout=args.timeout,
            interval=args.interval,
        )

    deadline = time.monotonic() + wait_seconds
    while True:
        view = load_run_view(
            controller_paths(args.controller_root, args.project_id),
            args.run_id,
        )
        changed = after_etag is None or view["etag"] != after_etag
        if changed or view["phase"] in {"attention_required", "missing", "purged"}:
            return {
                "changed": changed,
                "timed_out": False,
                "dispatcher_started": dispatcher_started,
                "run_view": view,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "changed": False,
                "timed_out": True,
                "dispatcher_started": dispatcher_started,
                "run_view": view,
            }
        time.sleep(min(0.5, remaining))


def _wait_runs_request() -> tuple[int, list[tuple[str, str | None]]]:
    payload = _read_object("wait-runs request")
    if set(payload) != {"schema_version", "wait_seconds", "runs"}:
        raise ValueError(
            "wait-runs request must contain schema_version, wait_seconds, and runs"
        )
    if isinstance(payload["schema_version"], bool) or payload["schema_version"] != 1:
        raise ValueError("unsupported wait-runs request schema")
    wait_seconds = payload["wait_seconds"]
    if (
        isinstance(wait_seconds, bool)
        or not isinstance(wait_seconds, int)
        or not 0 <= wait_seconds <= MAX_WAIT_SECONDS
    ):
        raise ValueError(f"wait_seconds must be between 0 and {MAX_WAIT_SECONDS}")
    raw_runs = payload["runs"]
    if not isinstance(raw_runs, list) or not 1 <= len(raw_runs) <= MAX_WAIT_RUNS:
        raise ValueError(f"runs must contain between 1 and {MAX_WAIT_RUNS} entries")

    requests: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for raw in raw_runs:
        if not isinstance(raw, dict) or set(raw) != {"run_id", "after_etag"}:
            raise ValueError("each wait-runs entry must contain run_id and after_etag")
        raw_run_id = raw["run_id"]
        if not isinstance(raw_run_id, str):
            raise ValueError("wait-runs run_id must be a string")
        run_id = validate_current_run_id(raw_run_id)
        if run_id in seen:
            raise ValueError(f"wait-runs contains duplicate run id: {run_id}")
        seen.add(run_id)
        after_etag = raw["after_etag"]
        if after_etag is not None and (
            not isinstance(after_etag, str) or ETAG_RE.fullmatch(after_etag) is None
        ):
            raise ValueError("after_etag must be null or a remote-runner run-view etag")
        requests.append((run_id, after_etag))
    return wait_seconds, requests


def wait_runs(args: argparse.Namespace) -> dict[str, Any]:
    wait_seconds, requests = _wait_runs_request()
    paths = controller_paths(args.controller_root, args.project_id)

    views = [load_run_view(paths, run_id) for run_id, _etag in requests]
    dispatcher_started = False
    if any(view["phase"] in ACTIVE_PHASES for view in views):
        dispatcher_started = ensure_dispatcher(
            controller_root=args.controller_root,
            project_id=args.project_id,
            timeout=args.timeout,
            interval=args.interval,
        )

    deadline = time.monotonic() + wait_seconds
    while True:
        views = [load_run_view(paths, run_id) for run_id, _etag in requests]
        changed_run_ids = [
            run_id
            for (run_id, after_etag), view in zip(requests, views, strict=True)
            if after_etag is None or view["etag"] != after_etag
        ]
        ready = cohort_report_readiness(views) != "waiting"
        if changed_run_ids or ready:
            return {
                "changed": bool(changed_run_ids),
                "changed_run_ids": changed_run_ids,
                "ready": ready,
                "timed_out": False,
                "dispatcher_started": dispatcher_started,
                "run_views": views,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "changed": False,
                "changed_run_ids": [],
                "ready": False,
                "timed_out": True,
                "dispatcher_started": dispatcher_started,
                "run_views": views,
            }
        time.sleep(min(0.5, remaining))


def configure_output_sync(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    config = store_config(paths.registry_root, _read_object("output-sync config"))
    worker_started = ensure_output_sync_worker(
        paths,
        timeout=args.timeout,
        interval=args.interval,
    )
    return {
        "configured": True,
        "target_server": config.target_server,
        "paused": config.paused,
        "worker_started": worker_started,
        "status": sync_status(paths.registry_root),
    }


def update_server_drain(args: argparse.Namespace, *, drained: bool) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    payload = _read_optional_object("server drain")
    if set(payload) - {"machine_id"}:
        raise ValueError("server drain payload is invalid")
    machine_id = payload.get("machine_id", args.server)
    if not isinstance(machine_id, str):
        raise ValueError("server drain machine_id must be a string")
    result = set_server_drained(
        paths,
        args.server,
        machine_id=machine_id,
        drained=drained,
    )
    project_queued_matches = sum(
        any(
            str(item["name"]) in job["eligible_servers"]
            and str(item.get("machine_id", item["name"])) == machine_id
            for item in job["prepared_servers"]
        )
        for job, _state in list_queued(paths)
    )
    dispatcher_started = False
    if not drained and project_queued_matches:
        dispatcher_started = ensure_dispatcher(
            controller_root=args.controller_root,
            project_id=args.project_id,
            timeout=args.timeout,
            interval=args.interval,
        )
    return {
        **result,
        "scope": "controller",
        "project_queued_matches": project_queued_matches,
        "dispatcher_started": dispatcher_started,
    }


def assess_server_retirement(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_object("server retirement assessment")
    if set(payload) != {"schema_version", "server"} or payload.get(
        "schema_version"
    ) != 1:
        raise ValueError("server retirement assessment payload is invalid")
    raw_server = payload.get("server")
    if not isinstance(raw_server, dict) or raw_server.get("name") != args.server:
        raise ValueError("server retirement assessment target is invalid")
    probe_payload = {
        "schema_version": 1,
        "servers": [{**raw_server, "enabled": True}],
    }
    server = validate_payload(probe_payload)[0]
    probe = collect_server_snapshot([server], timeout=args.timeout)[0]

    projects_root = args.controller_root.expanduser().resolve() / "projects"
    project_results: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    if projects_root.is_dir():
        project_entries = sorted(
            entry
            for entry in projects_root.iterdir()
            if entry.is_dir() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", entry.name)
        )
    else:
        project_entries = []

    for entry in project_entries:
        project_id = entry.name
        paths = controller_paths(args.controller_root, project_id)
        project: dict[str, Any] = {
            "project_id": project_id,
            "active_runs": [],
            "queued_runs": [],
            "pending_output_sync": [],
            "unverified_succeeded_outputs": [],
            "terminal_output_attention": [],
        }
        try:
            jobs: list[tuple[dict[str, Any], dict[str, Any]]] = []
            if paths.queue_dir.is_dir():
                for queue_entry in sorted(paths.queue_dir.iterdir()):
                    if not queue_entry.is_dir() or queue_entry.name.startswith("."):
                        continue
                    try:
                        jobs.append(load_job(paths, queue_entry.name))
                    except (OSError, RuntimeError, ValueError) as exc:
                        raise RuntimeError(
                            f"invalid controller queue record {queue_entry.name}: {exc}"
                        ) from exc
            for job, state in jobs:
                if state["status"] not in {"queued", "dispatching"}:
                    continue
                if args.server not in {
                    str(item["name"]) for item in eligible_prepared_servers(job)
                }:
                    continue
                project["queued_runs"].append(
                    {
                        "run_id": job["run_id"],
                        "task_id": job["task_id"],
                        "status": state["status"],
                    }
                )

            if paths.config_path.is_file():
                execution_paths = project_paths(paths.config_path)
                rows = monitoring.load_registry_rows(execution_paths)
                for row in rows:
                    if row.get("server") != args.server:
                        continue
                    run_id = row.get("run_id")
                    if not isinstance(run_id, str):
                        continue
                    status = row.get("authoritative_status") or row.get(
                        "stored_status"
                    )
                    if row.get("registry_kind") != "current":
                        if status not in {"succeeded", "failed", "stopped"}:
                            blockers.append(
                                {
                                    "code": "unsupported_active_execution",
                                    "project_id": project_id,
                                    "run_id": run_id,
                                }
                            )
                        continue
                    manifest, state = load_current_run(execution_paths, run_id)
                    status = state["status"]
                    if status not in {"succeeded", "failed", "stopped"}:
                        project["active_runs"].append(
                            {
                                "run_id": run_id,
                                "task_id": manifest["task_id"],
                                "status": status,
                            }
                        )
                        continue
                    output_path = manifest.get("output_path")
                    if output_path is None:
                        continue
                    if status == "succeeded":
                        sync = run_sync_status(paths.registry_root, run_id)
                        if sync.get("status") != "completed":
                            project["unverified_succeeded_outputs"].append(
                                {
                                    "run_id": run_id,
                                    "output_path": output_path,
                                    "output_sync": sync,
                                }
                            )
                    else:
                        project["terminal_output_attention"].append(
                            {
                                "run_id": run_id,
                                "status": status,
                                "output_path": output_path,
                            }
                        )

            project["pending_output_sync"] = [
                {
                    "run_id": intent["run_id"],
                    "source_path": intent["source_path"],
                }
                for intent in list_pending(paths.registry_root)
                if intent["source_server"] == args.server
            ]
        except (OSError, RuntimeError, ValueError) as exc:
            project["error"] = str(exc)
            blockers.append(
                {
                    "code": "project_assessment_failed",
                    "project_id": project_id,
                    "detail": str(exc),
                }
            )
        for field, code in (
            ("active_runs", "active_execution"),
            ("queued_runs", "queued_candidate"),
            ("pending_output_sync", "pending_output_sync"),
            ("unverified_succeeded_outputs", "unverified_succeeded_output"),
        ):
            for item in project[field]:
                blockers.append(
                    {"code": code, "project_id": project_id, **item}
                )
        for item in project["terminal_output_attention"]:
            attention.append(
                {"code": "terminal_output", "project_id": project_id, **item}
            )
        if any(
            project.get(field)
            for field in (
                "active_runs",
                "queued_runs",
                "pending_output_sync",
                "unverified_succeeded_outputs",
                "terminal_output_attention",
                "error",
            )
        ):
            project_results.append(project)

    probe_state = probe.get("state")
    if probe_state == "unreachable":
        blockers.append(
            {
                "code": "server_unreachable",
                "detail": probe.get("error", "server is unreachable"),
            }
        )
    elif probe_state not in {"idle", "busy"}:
        blockers.append(
            {"code": "server_probe_unsupported", "detail": str(probe_state)}
        )
    for active in probe.get("active_runs", []):
        blockers.append(
            {
                "code": "server_process_active",
                "run_id": active.get("run_id"),
                "workload_class": active.get("workload_class"),
            }
        )

    machine_id = str(server.get("machine_id", args.server))
    drained = machine_id in list_drained_servers(
        controller_paths(args.controller_root, args.project_id)
    )
    return {
        "schema_version": 1,
        "server": args.server,
        "ready": not blockers,
        "drained": drained,
        "probe": probe,
        "projects": project_results,
        "blockers": blockers,
        "attention": attention,
        "assessed_at": utc_now(),
    }


def dashboard(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_object("dashboard request")
    servers = validate_payload(payload)
    paths = controller_paths(args.controller_root, args.project_id)
    capacities = ensure_server_capacities(paths, servers)
    servers = [
        {
            **server,
            "standard_slots": capacities[server["machine_id"]]["standard_slots"],
            "test_slots": capacities[server["machine_id"]]["test_slots"],
            "capacity_revision": capacities[server["machine_id"]]["revision"],
            "capacity_customized": capacities[server["machine_id"]]["customized"],
        }
        for server in servers
    ]
    overview_args = argparse.Namespace(**vars(args))
    overview_args._full_overview = True
    with ThreadPoolExecutor(max_workers=2) as executor:
        overview_future = executor.submit(status, overview_args)
        snapshot_future = executor.submit(
            collect_server_snapshot,
            servers,
            timeout=args.timeout,
        )
        overview = overview_future.result()
        snapshot = snapshot_future.result()
    runs = overview.get("runs", [])
    if not isinstance(runs, list):
        raise RuntimeError("controller overview returned invalid runs")
    drains = overview.get("server_drains")
    if isinstance(drains, dict) and isinstance(drains.get("servers"), dict):
        by_machine = drains["servers"]
        overview["server_drains"] = {
            **drains,
            "servers": {
                str(server["name"]): by_machine[server["machine_id"]]
                for server in servers
                if server["machine_id"] in by_machine
            },
        }
    return {
        **overview,
        "servers": enrich_active_runs(snapshot, runs),
        "probe_interval_seconds": args.interval,
        "collected_at": utc_now(),
    }


def edit_server_capacity(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    payload = _read_object("server capacity update")
    if set(payload) != {
        "machine_id",
        "expected_revision",
        "standard_slots",
        "test_slots",
    }:
        raise ValueError("server capacity update payload is invalid")
    if not isinstance(payload["machine_id"], str):
        raise ValueError("server capacity machine_id must be a string")
    result = update_server_capacity(
        paths,
        args.server,
        machine_id=payload["machine_id"],
        expected_revision=payload["expected_revision"],
        standard_slots=payload["standard_slots"],
        test_slots=payload["test_slots"],
    )
    dispatcher_started = ensure_dispatcher(
        controller_root=args.controller_root,
        project_id=args.project_id,
        timeout=args.timeout,
        interval=args.interval,
    )
    return {**result, "dispatcher_started": dispatcher_started}


def stop(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    try:
        _job, queue_state = load_job(paths, args.run_id)
    except FileNotFoundError:
        queue_state = None
    if queue_state is not None and queue_state["status"] == "queued":
        updated = transition_queued_state(
            paths,
            args.run_id,
            expected_revision=int(queue_state["revision"]),
            status="stopped",
        )
        return {"kind": "queue", "state": updated}
    if queue_state is not None and queue_state["status"] == "dispatching":
        raise RuntimeError(
            "run is currently being dispatched; retry stop after dispatch resolves"
        )

    if paths.config_path.is_file():
        execution_paths = project_paths(paths.config_path)
        kind = registry_kind(execution_paths, args.run_id)
        if kind == "current":
            updated = stop_execution(execution_paths, args.run_id, args.timeout)
            result: dict[str, Any] = {"kind": "run", "state": updated}
            if queue_state is not None:
                result["queue_state"] = queue_state
            return result
        if kind is not None:
            raise ValueError(f"only current-format runs can be stopped; found {kind}")

    if queue_state is not None:
        return {"kind": "queue", "state": queue_state}
    raise FileNotFoundError(f"controller run does not exist: {args.run_id}")


def close_decommissioned_run(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    request = validate_request(_read_object("decommissioned-run request"))
    return inspect_or_close(
        paths,
        args.run_id,
        server=request["server"],
        reason=request["reason"],
        timeout=args.timeout,
        apply=args.apply,
    )


def _stopped_cleanup_candidates(paths: ControllerPaths) -> list[dict[str, Any]]:
    queue_by_id = {str(job["run_id"]): (job, state) for job, state in list_jobs(paths)}
    records: dict[str, dict[str, Any]] = {}
    for run_id, (job, state) in queue_by_id.items():
        if state["status"] != "stopped":
            continue
        records[run_id] = {
            "run_id": run_id,
            "label": job["label"],
            "task_id": job["task_id"],
            "queue_status": "stopped",
            "run_status": None,
            "server": None,
            "manifest": None,
        }

    if not paths.config_path.is_file():
        return list(records.values())
    execution_paths = project_paths(paths.config_path)
    if not execution_paths.runs_dir.is_dir():
        return list(records.values())
    for entry in execution_paths.runs_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            run_id = validate_current_run_id(entry.name)
        except ValueError:
            continue
        if registry_kind(execution_paths, run_id) != "current":
            continue
        manifest, state = load_current_run(execution_paths, run_id)
        if state["status"] != "stopped":
            continue
        queue = queue_by_id.get(run_id)
        queue_status = None if queue is None else str(queue[1]["status"])
        if queue_status is not None and queue_status not in QUEUE_TERMINAL:
            raise RuntimeError(
                f"stopped execution {run_id} has non-terminal queue state {queue_status}"
            )
        record = records.setdefault(
            run_id,
            {
                "run_id": run_id,
                "label": manifest["label"],
                "task_id": manifest["task_id"],
                "queue_status": queue_status,
                "run_status": None,
                "server": manifest["server"],
                "manifest": None,
            },
        )
        record.update(
            {
                "queue_status": queue_status,
                "run_status": "stopped",
                "server": manifest["server"],
                "manifest": manifest,
            }
        )
    return sorted(records.values(), key=lambda item: str(item["run_id"]))


def cleanup_records(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    candidates = _stopped_cleanup_candidates(paths)
    if args.run_id is not None:
        validate_current_run_id(args.run_id)
        candidates = [item for item in candidates if item["run_id"] == args.run_id]
        if not candidates:
            raise ValueError(f"run is not an active stopped record: {args.run_id}")

    public = [
        {key: value for key, value in item.items() if key != "manifest"}
        for item in candidates
    ]
    if not args.apply:
        return {
            "applied": False,
            "candidate_count": len(public),
            "candidates": public,
        }

    execution_paths = (
        project_paths(paths.config_path) if paths.config_path.is_file() else None
    )
    results: list[dict[str, Any]] = []
    for item in candidates:
        run_id = str(item["run_id"])
        result: dict[str, Any] = {"run_id": run_id, "status": "purged"}
        try:
            manifest = item["manifest"]
            if manifest is not None:
                result["runtime"] = cleanup_remote_runtime(
                    str(manifest["ssh"]),
                    str(manifest["project_python"]),
                    run_id,
                    args.timeout,
                )
            if item["queue_status"] is not None:
                allowed = (
                    QUEUE_TERMINAL
                    if item["run_status"] == "stopped"
                    else frozenset({"stopped"})
                )
                result["queue_purged"] = str(
                    purge_queue_entry(
                        paths, run_id, allowed_statuses=frozenset(allowed)
                    )
                )
            if item["run_status"] == "stopped":
                if execution_paths is None:
                    raise RuntimeError("controller project config is unavailable")
                result["run_purged"] = str(purge_current_run(execution_paths, run_id))
        except (
            CleanupOutcomeUnknown,
            FileNotFoundError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            result.update({"status": "failed", "error": str(exc)})
        results.append(result)
    return {
        "applied": True,
        "candidate_count": len(public),
        "purged_count": sum(item["status"] == "purged" for item in results),
        "failed_count": sum(item["status"] == "failed" for item in results),
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate a remote-runner controller registry."
    )
    parser.add_argument("--controller-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--interval", type=int, default=60)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("submit")
    subparsers.add_parser("pending-all")
    subparsers.add_parser("extend-all")
    queued_job_parser = subparsers.add_parser("queued-job")
    queued_job_parser.add_argument("--run-id", required=True)
    extend_job_parser = subparsers.add_parser("extend-job")
    extend_job_parser.add_argument("--run-id", required=True)
    edit_job_parser = subparsers.add_parser("update-queued-job")
    edit_job_parser.add_argument("--run-id", required=True)
    capacity_parser = subparsers.add_parser("update-server-capacity")
    capacity_parser.add_argument("--server", required=True)
    reserve_job_parser = subparsers.add_parser("reserve-queue-update")
    reserve_job_parser.add_argument("--run-id", required=True)
    release_job_parser = subparsers.add_parser("release-queue-update")
    release_job_parser.add_argument("--run-id", required=True)
    status_parser = subparsers.add_parser("status")
    status_selector = status_parser.add_mutually_exclusive_group()
    status_selector.add_argument("--run-id")
    status_selector.add_argument("--task-id")
    wait_parser = subparsers.add_parser("wait-run")
    wait_parser.add_argument("--run-id", required=True)
    wait_parser.add_argument("--after-etag")
    wait_parser.add_argument("--wait-seconds", type=int, default=50)
    subparsers.add_parser("wait-runs")
    dashboard_parser = subparsers.add_parser("dashboard")
    dashboard_parser.set_defaults(run_id=None, task_id=None)
    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("--run-id", required=True)
    decommissioned_parser = subparsers.add_parser("close-decommissioned-run")
    decommissioned_parser.add_argument("--run-id", required=True)
    decommissioned_parser.add_argument("--apply", action="store_true")
    cleanup_parser = subparsers.add_parser("cleanup-stopped")
    cleanup_parser.add_argument("--run-id")
    cleanup_parser.add_argument("--apply", action="store_true")
    run_purge_parser = subparsers.add_parser("purge-run")
    run_purge_parser.add_argument("--run-id", required=True)
    run_purge_parser.add_argument("--reason", required=True)
    run_purge_parser.add_argument("--apply", action="store_true")
    run_purge_parser.add_argument("--delete-artifacts", action="store_true")
    purge_parser = subparsers.add_parser("purge-task")
    purge_parser.add_argument("--task-id", required=True)
    purge_parser.add_argument("--reason", required=True)
    purge_parser.add_argument("--apply", action="store_true")
    purge_parser.add_argument("--delete-artifacts", action="store_true")
    subparsers.add_parser("configure-output-sync")
    output_prune_parser = subparsers.add_parser("prune-outputs")
    output_prune_parser.add_argument("--run-id")
    output_prune_parser.add_argument("--server", action="append")
    output_prune_parser.add_argument("--apply", action="store_true")
    drain_parser = subparsers.add_parser("drain-server")
    drain_parser.add_argument("--server", required=True)
    resume_parser = subparsers.add_parser("resume-server")
    resume_parser.add_argument("--server", required=True)
    retirement_parser = subparsers.add_parser("assess-server-retirement")
    retirement_parser.add_argument("--server", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "submit":
            result = submit(args)
        elif args.action == "pending-all":
            result = pending_all(args)
        elif args.action == "extend-all":
            result = extend_all(args)
        elif args.action == "queued-job":
            result = queued_job(args)
        elif args.action == "extend-job":
            result = extend_job(args)
        elif args.action == "update-queued-job":
            result = edit_queued_job(args)
        elif args.action == "update-server-capacity":
            result = edit_server_capacity(args)
        elif args.action == "reserve-queue-update":
            result = reserve_queue_update(args)
        elif args.action == "release-queue-update":
            result = release_queue_update(args)
        elif args.action == "status":
            result = status(args)
        elif args.action == "wait-run":
            result = wait_run(args)
        elif args.action == "wait-runs":
            result = wait_runs(args)
        elif args.action == "dashboard":
            result = dashboard(args)
        elif args.action == "configure-output-sync":
            result = configure_output_sync(args)
        elif args.action == "prune-outputs":
            result = prune_outputs(args)
        elif args.action == "drain-server":
            result = update_server_drain(args, drained=True)
        elif args.action == "resume-server":
            result = update_server_drain(args, drained=False)
        elif args.action == "assess-server-retirement":
            result = assess_server_retirement(args)
        elif args.action == "stop":
            result = stop(args)
        elif args.action == "close-decommissioned-run":
            result = close_decommissioned_run(args)
        elif args.action == "cleanup-stopped":
            result = cleanup_records(args)
        elif args.action == "purge-run":
            result = purge_run(args)
        elif args.action == "purge-task":
            result = purge_task(args)
        else:
            raise AssertionError(f"unhandled controller action: {args.action}")
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
