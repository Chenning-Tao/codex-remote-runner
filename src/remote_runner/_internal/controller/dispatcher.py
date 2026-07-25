from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import launch, monitoring, registration
from ..execution_registry import (
    TERMINAL_STATUSES,
    load_current_run,
    project_paths,
    registry_kind,
    update_current_state,
    utc_now,
    write_yaml,
)
from ..output_paths import resolve_output_path
from ..remote_shell import remote_python_stdin_command, ssh_connection_options
from ..scheduling import CapacityCandidate, rank_candidates, resolve_worker_command
from ..worktree import prepare_remote_worktree
from .registry import (
    ControllerPaths,
    acquire_dispatch_lease,
    controller_paths,
    eligible_prepared_servers,
    has_unexpired_dispatch_lease,
    list_drained_servers,
    list_jobs,
    list_queued,
    placement_update_active,
    recover_dispatching_state,
    release_dispatch_lease,
    transition_queued_state,
)
from .output_sync_worker import ensure_output_sync_worker


SERVER_STATE_PROBE_PROGRAM = r"""import json
import os
import subprocess
from pathlib import Path


def exact_tmux_target(session_name):
    return "=" + session_name

active = []
root = Path.home() / ".rr"
if root.is_dir():
    for status_path in root.glob("rr-*/status.json"):
        try:
            value = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("state") != "running":
            continue
        runtime = status_path.parent
        alive = False
        try:
            pgid = int((runtime / "pgid").read_text(encoding="utf-8").strip())
            if pgid > 1:
                os.killpg(pgid, 0)
                alive = True
        except PermissionError:
            alive = True
        except (FileNotFoundError, OSError, ValueError):
            pass
        if not alive:
            alive = subprocess.run(
                ["tmux", "has-session", "-t", exact_tmux_target(status_path.parent.name)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0
        if alive:
            workload_class = value.get("workload_class", "standard")
            if workload_class not in {"standard", "test"}:
                workload_class = "standard"
            item = {
                "run_id": str(value.get("run_id", status_path.parent.name)),
                "workload_class": workload_class,
            }
            label = value.get("label")
            if isinstance(label, str) and label:
                item["label"] = label
            active.append(item)
load1, load5, load15 = os.getloadavg()
print(json.dumps({
    "active_runs": sorted(active, key=lambda item: item["run_id"]),
    "active_run_ids": sorted(item["run_id"] for item in active),
    "load1": load1,
    "load5": load5,
    "load15": load15,
    "remote_cores": os.cpu_count(),
}))
"""
MAX_CAPACITY_PROBE_WORKERS = 8


@dataclass(frozen=True)
class DispatchOutcome:
    action: str
    run_id: str | None
    server: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ProbedServer:
    server: dict[str, Any]
    capacity: CapacityCandidate
    active_standard_count: int
    active_test_count: int


def probe_server_state(ssh: str, python: str, timeout: int) -> dict[str, Any]:
    argv = [
        "ssh",
        *ssh_connection_options(timeout),
        ssh,
        remote_python_stdin_command(python),
    ]
    try:
        completed = subprocess.run(
            argv,
            input=SERVER_STATE_PROBE_PROGRAM.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"active-run probe timed out after {exc.timeout}s") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.decode(errors="replace").strip()
            or f"active-run probe exited {completed.returncode}"
        )
    try:
        value = json.loads(completed.stdout.decode())
        active_runs = value["active_runs"]
        load1 = float(value["load1"])
        load5 = float(value["load5"])
        load15 = float(value["load15"])
        remote_cores = value.get("remote_cores")
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("active-run probe returned invalid JSON") from exc
    if not isinstance(active_runs, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("run_id"), str)
        and item.get("workload_class") in {"standard", "test"}
        for item in active_runs
    ):
        raise RuntimeError("active-run probe returned invalid active runs")
    normalized = tuple(
        {
            "run_id": str(item["run_id"]),
            "workload_class": str(item["workload_class"]),
            **(
                {"label": str(item["label"])}
                if isinstance(item.get("label"), str) and item["label"]
                else {}
            ),
        }
        for item in active_runs
    )
    return {
        "reachable": True,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "remote_cores": remote_cores,
        "active_runs": normalized,
        "active_run_ids": tuple(item["run_id"] for item in normalized),
    }


def _active_runs(probe: dict[str, Any]) -> tuple[dict[str, str], ...]:
    raw = probe.get("active_runs")
    if raw is None:
        raw = tuple(
            {"run_id": str(run_id), "workload_class": "standard"}
            for run_id in probe.get("active_run_ids", ())
        )
    if not isinstance(raw, (list, tuple)) or not all(
        isinstance(item, dict)
        and isinstance(item.get("run_id"), str)
        and item.get("workload_class") in {"standard", "test"}
        for item in raw
    ):
        raise RuntimeError("active-run probe returned invalid active runs")
    return tuple(
        {
            "run_id": str(item["run_id"]),
            "workload_class": str(item["workload_class"]),
        }
        for item in raw
    )


def _probe_prepared_server(server: dict[str, Any], timeout: int) -> ProbedServer:
    probe = probe_server_state(
        str(server["ssh"]),
        str(server["python"]),
        timeout,
    )
    active = _active_runs(probe)
    standard_count = sum(item["workload_class"] == "standard" for item in active)
    test_count = sum(item["workload_class"] == "test" for item in active)
    return ProbedServer(
        server=server,
        capacity=CapacityCandidate(
            name=str(server["name"]),
            configured_cores=int(server["configured_cores"]),
            load5=float(probe["load5"]),
            priority=int(server["priority"]),
            active_run_count=standard_count,
        ),
        active_standard_count=standard_count,
        active_test_count=test_count,
    )


def _probe_prepared_servers(
    servers: list[dict[str, Any]],
    timeout: int,
) -> tuple[list[ProbedServer], list[str]]:
    def probe(server: dict[str, Any]) -> tuple[ProbedServer | None, str | None]:
        try:
            return _probe_prepared_server(server, timeout), None
        except RuntimeError as exc:
            return None, f"{server['name']}: {exc}"

    if len(servers) <= 1:
        results = [probe(server) for server in servers]
    else:
        workers = min(MAX_CAPACITY_PROBE_WORKERS, len(servers))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(probe, servers))
    reachable = [result for result, _failure in results if result is not None]
    failures = [failure for _result, failure in results if failure is not None]
    return reachable, failures


def _has_workload_capacity(workload_class: str, candidate: ProbedServer) -> bool:
    if workload_class == "standard":
        return candidate.active_standard_count == 0
    return candidate.active_test_count < int(candidate.server.get("test_slots", 0))


def _capacity_message(workload_class: str, candidates: list[ProbedServer]) -> str:
    if workload_class == "standard":
        return "runner capacity saturated"
    used = max((candidate.active_test_count for candidate in candidates), default=0)
    total = max(
        (int(candidate.server.get("test_slots", 0)) for candidate in candidates),
        default=0,
    )
    return f"test slots full ({used}/{total})"


def _rank_for_workload(
    workload_class: str,
    candidates: list[ProbedServer],
) -> list[ProbedServer]:
    eligible = [
        candidate
        for candidate in candidates
        if _has_workload_capacity(workload_class, candidate)
    ]
    if not eligible:
        return eligible
    by_name = {candidate.capacity.name: candidate for candidate in eligible}
    return [
        by_name[item.name]
        for item in rank_candidates([item.capacity for item in eligible])
    ]


def _select_server_for_job(
    paths: ControllerPaths,
    job: dict[str, Any],
    *,
    timeout: int,
    allowed_server_names: set[str] | None = None,
) -> tuple[ProbedServer | None, str]:
    workload_class = str(job["workload_class"])
    drained_servers = set(list_drained_servers(paths))
    eligible_servers = []
    for server in eligible_prepared_servers(job):
        if str(server["name"]) in drained_servers:
            continue
        if (
            allowed_server_names is not None
            and str(server["name"]) not in allowed_server_names
        ):
            continue
        eligible_servers.append(server)
    reachable, failures = _probe_prepared_servers(eligible_servers, timeout)
    if not reachable:
        eligible_names = {
            str(server["name"]) for server in eligible_prepared_servers(job)
        }
        if eligible_names and eligible_names <= drained_servers:
            return None, "all prepared servers are drained"
        return None, "; ".join(failures) or "no reachable prepared server"

    ranked = _rank_for_workload(workload_class, reachable)
    if not ranked:
        return None, _capacity_message(workload_class, reachable)

    ttl = int(job.get("lease_seconds", 120))
    acquired = False
    latest = reachable
    for candidate in ranked:
        if not acquire_dispatch_lease(
            paths,
            server=candidate.capacity.name,
            run_id=str(job["run_id"]),
            ttl_seconds=ttl,
        ):
            continue
        acquired = True
        try:
            current = _probe_prepared_server(candidate.server, timeout)
        except RuntimeError:
            release_dispatch_lease(
                paths,
                server=candidate.capacity.name,
                run_id=str(job["run_id"]),
            )
            continue
        if _has_workload_capacity(workload_class, current):
            return current, ""
        latest = [current]
        release_dispatch_lease(
            paths,
            server=candidate.capacity.name,
            run_id=str(job["run_id"]),
        )
    if acquired:
        return None, _capacity_message(workload_class, latest)
    return None, "dispatch leases busy"


def _select_backfill_from_lane(
    paths: ControllerPaths,
    lane: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    timeout: int,
) -> tuple[
    ProbedServer | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    head_job, _head_state = lane[0]
    drained_servers = set(list_drained_servers(paths))
    # Blocked jobs reserve every server they could use; backfill only elsewhere.
    protected_servers = {
        str(server["name"])
        for server in eligible_prepared_servers(head_job)
        if str(server["name"]) not in drained_servers
    }

    for candidate_job, candidate_state in lane[1:]:
        eligible_servers = {
            str(server["name"])
            for server in eligible_prepared_servers(candidate_job)
            if str(server["name"]) not in drained_servers
        }
        safe_servers = eligible_servers - protected_servers
        if safe_servers:
            selected, _message = _select_server_for_job(
                paths,
                candidate_job,
                timeout=timeout,
                allowed_server_names=safe_servers,
            )
        else:
            selected = None
        if selected is not None:
            return selected, candidate_job, candidate_state
        protected_servers.update(eligible_servers)

    return None, None, None


def _ensure_controller_anchor(paths: ControllerPaths) -> None:
    if paths.config_path.is_file():
        return
    paths.project_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.registry_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_yaml(paths.config_path, {"controller_registry": True})


def _register_execution(
    paths: ControllerPaths,
    job: dict[str, Any],
    server: dict[str, Any],
    *,
    workdir: str,
    resolved_command: str,
    workers: int | None,
    worker_defaulted: bool,
    output_root: str | None,
    output_relpath: str | None,
    output_path: str | None,
) -> None:
    _ensure_controller_anchor(paths)
    args = argparse.Namespace(
        project_config=paths.config_path,
        label=job["label"],
        task_id=job["task_id"],
        result_intent=job["result_intent"],
        result_tags=job["result_tags"],
        workload_class=job.get("workload_class", "standard"),
        server=server["name"],
        ssh=server["ssh"],
        ssh_profile=server["ssh_profile"],
        configured_cores=server["configured_cores"],
        minimum_cores=job["minimum_cores"],
        workers=workers,
        command=resolved_command,
        remote_workdir=workdir,
        project_python=server["python"],
        source_revision=job["revision"],
        prepared_servers=[item["name"] for item in eligible_prepared_servers(job)],
        submitted_command=job["submitted_command"],
        worker_defaulted=worker_defaulted,
        expected_revision=job["revision"],
        require_clean_worktree=True,
        output_root=output_root,
        output_relpath=output_relpath,
        output_path=output_path,
        output_metadata=json.dumps(job.get("output_metadata", {}), sort_keys=True),
        run_id=job["run_id"],
        privacy=job.get("privacy"),
    )
    registration.register(args)


def _resolve_selected_output(
    job: dict[str, Any],
    server: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    output_relpath = job.get("output_relpath")
    if output_relpath is None:
        return None, None, job.get("output_path")
    output_root = server.get("output_root")
    output_path = resolve_output_path(output_root, output_relpath)
    return str(output_root), str(output_relpath), output_path


def _fail_registered_execution(paths: ControllerPaths, run_id: str, error: str) -> None:
    if not paths.config_path.is_file():
        return
    execution_paths = project_paths(paths.config_path)
    if registry_kind(execution_paths, run_id) != "current":
        return
    _manifest, state = load_current_run(execution_paths, run_id)
    if state["status"] != "registered":
        return
    update_current_state(
        execution_paths,
        run_id,
        int(state["revision"]),
        {
            "status": "failed",
            "finished_at": utc_now(),
            "error": error,
        },
        action="controller_dispatch_failed",
    )


def dispatch_once(paths: ControllerPaths, *, timeout: int = 8) -> DispatchOutcome:
    while True:
        dispatching = list_jobs(paths, statuses={"dispatching"})
        if not dispatching:
            break
        job, state = dispatching[0]
        run_id = str(job["run_id"])
        if paths.config_path.is_file():
            execution_paths = project_paths(paths.config_path)
            if registry_kind(execution_paths, run_id) == "current":
                transition_queued_state(
                    paths,
                    run_id,
                    expected_revision=int(state["revision"]),
                    status="dispatched",
                )
                continue
        if has_unexpired_dispatch_lease(paths, run_id=run_id):
            return DispatchOutcome(action="busy", run_id=run_id)
        recover_dispatching_state(
            paths,
            run_id,
            expected_revision=int(state["revision"]),
        )

    queued = [
        row for row in list_queued(paths) if not placement_update_active(row[1])
    ]
    if not queued:
        return DispatchOutcome(action="idle", run_id=None)
    lanes: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []
    for workload_class in ("standard", "test"):
        lane = [row for row in queued if row[0]["workload_class"] == workload_class]
        if lane:
            lanes.append(lane)

    blocked: list[tuple[str, str, str]] = []
    selected: ProbedServer | None = None
    job: dict[str, Any] | None = None
    state: dict[str, Any] | None = None
    for lane in lanes:
        candidate_job, candidate_state = lane[0]
        selected, message = _select_server_for_job(
            paths,
            candidate_job,
            timeout=timeout,
        )
        if selected is not None:
            job = candidate_job
            state = candidate_state
            break
        blocked.append(
            (
                str(candidate_job["workload_class"]),
                str(candidate_job["run_id"]),
                message,
            )
        )
    if selected is None:
        for lane in lanes:
            selected, candidate_job, candidate_state = _select_backfill_from_lane(
                paths,
                lane,
                timeout=timeout,
            )
            if (
                selected is not None
                and candidate_job is not None
                and candidate_state is not None
            ):
                job = candidate_job
                state = candidate_state
                break
    if selected is None or job is None or state is None:
        workload_class, run_id, message = blocked[0]
        if len(blocked) > 1:
            message = "; ".join(f"{kind}: {detail}" for kind, _run, detail in blocked)
        return DispatchOutcome(action="queued", run_id=run_id, message=message)

    run_id = str(job["run_id"])
    selected_server = selected.server
    selected_capacity = selected.capacity

    try:
        transition_queued_state(
            paths,
            run_id,
            expected_revision=int(state["revision"]),
            status="dispatching",
        )
    except RuntimeError as exc:
        release_dispatch_lease(
            paths,
            server=selected_capacity.name,
            run_id=run_id,
        )
        if str(exc) == "queued state revision conflict":
            return dispatch_once(paths, timeout=timeout)
        raise
    execution_registered = False
    release_lease = True
    try:
        output_root, output_relpath, output_path = _resolve_selected_output(
            job,
            selected_server,
        )
        worktree = prepare_remote_worktree(
            ssh=str(selected_server["ssh"]),
            python=str(selected_server["python"]),
            bare_repo=str(selected_server["bare_repo"]),
            worktree_root=str(selected_server["worktree_root"]),
            revision=str(job["revision"]),
            timeout=timeout,
        )
        if job["workload_class"] == "test":
            resolved_command = str(job["submitted_command"])
            workers = None
            defaulted = False
        else:
            resolved_command, workers, defaulted = resolve_worker_command(
                str(job["submitted_command"]),
                worker_arg=str(job["worker_arg"]),
                configured_cores=selected_capacity.configured_cores,
            )
        _register_execution(
            paths,
            job,
            selected_server,
            workdir=worktree.workdir,
            resolved_command=resolved_command,
            workers=workers,
            worker_defaulted=defaulted,
            output_root=output_root,
            output_relpath=output_relpath,
            output_path=output_path,
        )
        execution_registered = True
        execution_paths = project_paths(paths.config_path)
        launch.launch(execution_paths, run_id, timeout)
        transition_queued_state(
            paths,
            run_id,
            expected_revision=int(state["revision"]) + 1,
            status="dispatched",
        )
        return DispatchOutcome(
            action="started", run_id=run_id, server=selected_capacity.name
        )
    except Exception as exc:
        unknown_launch = execution_registered and isinstance(
            exc.__cause__,
            (launch.BootstrapOutcomeUnknown, OSError),
        )
        if unknown_launch:
            release_lease = False
            transition_queued_state(
                paths,
                run_id,
                expected_revision=int(state["revision"]) + 1,
                status="dispatched",
                error=str(exc),
            )
            return DispatchOutcome(
                action="unknown",
                run_id=run_id,
                server=selected_capacity.name,
                message=str(exc),
            )
        if execution_registered:
            _fail_registered_execution(paths, run_id, str(exc))
        transition_queued_state(
            paths,
            run_id,
            expected_revision=int(state["revision"]) + 1,
            status="failed",
            error=str(exc),
        )
        return DispatchOutcome(
            action="failed",
            run_id=run_id,
            server=selected_capacity.name,
            message=str(exc),
        )
    finally:
        if release_lease:
            release_dispatch_lease(paths, server=selected_capacity.name, run_id=run_id)


def dispatch_loop(
    paths: ControllerPaths,
    *,
    timeout: int = 8,
    interval_seconds: int = 60,
) -> int:
    if interval_seconds <= 0:
        raise ValueError("dispatcher interval must be positive")
    while True:
        active = False
        if paths.config_path.is_file():
            execution_paths = project_paths(paths.config_path)
            rows = [
                row
                for row in monitoring.load_registry_rows(execution_paths)
                if row.get("registry_kind") == "current"
            ]
            for monitored in monitoring.monitor_rows(
                execution_paths,
                rows,
                timeout,
                no_write=False,
            ):
                if monitored.get("authoritative_status") not in TERMINAL_STATUSES:
                    active = True
        while True:
            outcome = dispatch_once(paths, timeout=timeout)
            if outcome.action != "started":
                break
            active = True
        try:
            ensure_output_sync_worker(
                paths,
                timeout=timeout,
                interval=interval_seconds,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            print(
                f"output-sync worker start failed: {exc}", file=sys.stderr, flush=True
            )
        if outcome.action == "idle" and not active:
            return 0
        time.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch queued remote-runner jobs.")
    parser.add_argument("--controller-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    paths = controller_paths(args.controller_root, args.project_id)
    if args.once:
        print(
            json.dumps(
                dispatch_once(paths, timeout=args.timeout).__dict__, sort_keys=True
            )
        )
        return 0
    return dispatch_loop(
        paths,
        timeout=args.timeout,
        interval_seconds=args.interval,
    )


if __name__ == "__main__":
    raise SystemExit(main())
