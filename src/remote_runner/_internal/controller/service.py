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
    run_sync_status,
    store_config,
    sync_status,
)
from ..result_metadata import MONITOR_RESULT_INTENTS
from ..run_readiness import cohort_report_readiness
from ..scheduling import normalize_workload_class
from ..stopping import stop as stop_execution
from ..tmux import dispatcher_tmux_session, exact_tmux_target, resolve_tmux_executable
from .dashboard import collect_server_snapshot, enrich_active_runs, validate_payload
from .experiments import (
    binding_submission_guard,
    ingest_binding as ingest_experiment_binding,
    ingest_completed_sync_results,
    ingest_result as ingest_experiment_result,
    preview_plan as preview_experiment_plan,
    publish_plan as publish_experiment_plan,
    query_registry as query_experiment_registry,
    rebuild_registry as rebuild_experiment_registry,
    record_acceptance as record_experiment_acceptance,
)
from .output_prune import prune_outputs
from .run_purge import purge_run
from .task_purge import purge_task
from .registry import (
    ControllerPaths,
    QUEUE_TERMINAL,
    controller_paths,
    ensure_server_capacities,
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


def submit(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    job = _read_object("job")
    if "output_sync" in job:
        output_sync = job.get("output_sync")
        if output_sync is None:
            disable_config(paths.registry_root)
        else:
            store_config(paths.registry_root, output_sync)
    raw_binding = job.get("experiment_binding")
    if raw_binding is None:
        entry = submit_job(paths, job)
    else:
        with binding_submission_guard(paths, raw_binding) as binding:
            job["experiment_binding"] = binding
            entry = submit_job(paths, job)
    stored_job, _state = load_job(paths, entry.name)
    ensure_server_capacities(paths, stored_job["prepared_servers"])
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


def experiment_query(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    ingestion = ingest_completed_sync_results(paths)
    result = query_experiment_registry(paths, _read_object("experiment query"))
    result["ingestion"] = {
        "projected": ingestion["projected"],
        "error_count": len(ingestion["errors"]),
        "errors": ingestion["errors"][:20],
    }
    return result


def experiment_plan_preview(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    return preview_experiment_plan(paths, _read_object("experiment plan"))


def experiment_plan_publish(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    request = _read_object("experiment plan publication")
    if set(request) - {"plan", "request_id", "expected_impact_digest"}:
        raise ValueError("experiment plan publication contains unknown fields")
    if "plan" not in request or "request_id" not in request:
        raise ValueError("experiment plan publication requires plan and request_id")
    request_id = request["request_id"]
    if not isinstance(request_id, str):
        raise ValueError("experiment plan publication request_id must be a string")
    expected_impact_digest = request.get("expected_impact_digest")
    if expected_impact_digest is not None and not isinstance(
        expected_impact_digest, str
    ):
        raise ValueError("expected_impact_digest must be a string or null")
    return publish_experiment_plan(
        paths,
        request["plan"],
        request_id=request_id,
        expected_impact_digest=expected_impact_digest,
    )


def experiment_binding_ingest(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    return ingest_experiment_binding(paths, _read_object("run binding"))


def experiment_result_ingest(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    return ingest_experiment_result(paths, _read_object("experiment result"))


def experiment_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    ingest_completed_sync_results(paths)
    return record_experiment_acceptance(paths, _read_object("acceptance request"))


def experiment_registry_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    return rebuild_experiment_registry(paths)


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
    result = extend_queued_job(
        paths,
        args.run_id,
        revision=revision,
        prepared_servers=prepared_servers,
        placement_token=placement_token,
    )
    ensure_server_capacities(paths, prepared_servers)
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
        slot_field = f"{target_class}_slots"
        if not any(
            isinstance(name, str)
            and int(capacities.get(name, {}).get(slot_field, 0)) > 0
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
            "result_intent",
            "workload_class",
            "worker_policy",
            "queue_priority",
            "queue_position",
            "minimum_cores",
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
            "result_intent",
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
            "by_result_intent": _status_counts(
                [str(job.get("result_intent", "unclassified")) for job, _state in jobs]
            ),
        },
        "runs": {
            "total": len(rows),
            "active": sum(_active_execution(row) for row in rows),
            "matched": runs_matched,
            "returned": runs_returned,
            "omitted": max(0, runs_matched - runs_returned),
            "by_authoritative_status": _status_counts(run_statuses),
            "by_result_intent": _status_counts(
                [str(row.get("result_intent", "unclassified")) for row in rows]
            ),
        },
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    task_selector = _task_selector(getattr(args, "task_id", None))
    result_intent = getattr(args, "result_intent", None)
    overview = args.run_id is None and task_selector is None
    all_jobs = [] if args.run_id is not None else list_jobs(paths)
    if result_intent is not None:
        all_jobs = [
            item for item in all_jobs if item[0].get("result_intent") == result_intent
        ]
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
    if result_intent is not None:
        queue = [
            item for item in queue if item["job"].get("result_intent") == result_intent
        ]
    runs: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []
    if paths.config_path.is_file():
        execution_paths = project_paths(paths.config_path)
        all_rows = monitoring.load_registry_rows(
            execution_paths, only_run_id=args.run_id
        )
        if result_intent is not None:
            all_rows = [
                row for row in all_rows if row.get("result_intent") == result_intent
            ]
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
    result = set_server_drained(paths, args.server, drained=drained)
    project_queued_matches = sum(
        args.server in {str(item["name"]) for item in job["prepared_servers"]}
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


def dashboard(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_object("dashboard request")
    servers = validate_payload(payload)
    paths = controller_paths(args.controller_root, args.project_id)
    capacities = ensure_server_capacities(paths, servers)
    servers = [
        {
            **server,
            "standard_slots": capacities[server["name"]]["standard_slots"],
            "test_slots": capacities[server["name"]]["test_slots"],
            "capacity_revision": capacities[server["name"]]["revision"],
            "capacity_customized": capacities[server["name"]]["customized"],
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
    return {
        **overview,
        "servers": enrich_active_runs(snapshot, runs),
        "probe_interval_seconds": args.interval,
        "collected_at": utc_now(),
    }


def edit_server_capacity(args: argparse.Namespace) -> dict[str, Any]:
    paths = controller_paths(args.controller_root, args.project_id)
    payload = _read_object("server capacity update")
    if set(payload) != {"expected_revision", "standard_slots", "test_slots"}:
        raise ValueError("server capacity update payload is invalid")
    result = update_server_capacity(
        paths,
        args.server,
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
    subparsers.add_parser("experiment-query")
    subparsers.add_parser("experiment-plan-preview")
    subparsers.add_parser("experiment-plan-publish")
    subparsers.add_parser("experiment-binding-ingest")
    subparsers.add_parser("experiment-result-ingest")
    subparsers.add_parser("experiment-acceptance")
    subparsers.add_parser("experiment-registry-rebuild")
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
    status_parser.add_argument("--result-intent", choices=MONITOR_RESULT_INTENTS)
    wait_parser = subparsers.add_parser("wait-run")
    wait_parser.add_argument("--run-id", required=True)
    wait_parser.add_argument("--after-etag")
    wait_parser.add_argument("--wait-seconds", type=int, default=50)
    subparsers.add_parser("wait-runs")
    dashboard_parser = subparsers.add_parser("dashboard")
    dashboard_parser.set_defaults(run_id=None, task_id=None, result_intent=None)
    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("--run-id", required=True)
    cleanup_parser = subparsers.add_parser("cleanup-stopped")
    cleanup_parser.add_argument("--run-id")
    cleanup_parser.add_argument("--apply", action="store_true")
    run_purge_parser = subparsers.add_parser("purge-run")
    run_purge_parser.add_argument("--run-id", required=True)
    replacement = run_purge_parser.add_mutually_exclusive_group(required=True)
    replacement.add_argument("--replacement-run-id")
    replacement.add_argument("--no-replacement", action="store_true")
    run_purge_parser.add_argument("--reason", required=True)
    run_purge_parser.add_argument("--apply", action="store_true")
    purge_parser = subparsers.add_parser("purge-task")
    purge_parser.add_argument("--task-id", required=True)
    purge_parser.add_argument("--reason", required=True)
    purge_parser.add_argument("--apply", action="store_true")
    subparsers.add_parser("configure-output-sync")
    output_prune_parser = subparsers.add_parser("prune-outputs")
    output_prune_parser.add_argument("--run-id")
    output_prune_parser.add_argument("--server", action="append")
    output_prune_parser.add_argument("--apply", action="store_true")
    drain_parser = subparsers.add_parser("drain-server")
    drain_parser.add_argument("--server", required=True)
    resume_parser = subparsers.add_parser("resume-server")
    resume_parser.add_argument("--server", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "submit":
            result = submit(args)
        elif args.action == "experiment-query":
            result = experiment_query(args)
        elif args.action == "experiment-plan-preview":
            result = experiment_plan_preview(args)
        elif args.action == "experiment-plan-publish":
            result = experiment_plan_publish(args)
        elif args.action == "experiment-binding-ingest":
            result = experiment_binding_ingest(args)
        elif args.action == "experiment-result-ingest":
            result = experiment_result_ingest(args)
        elif args.action == "experiment-acceptance":
            result = experiment_acceptance(args)
        elif args.action == "experiment-registry-rebuild":
            result = experiment_registry_rebuild(args)
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
        elif args.action == "stop":
            result = stop(args)
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
