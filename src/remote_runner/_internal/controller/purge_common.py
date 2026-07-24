from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any

from .. import monitoring
from ..execution_registry import load_current_run, project_paths
from .registry import (
    acquire_maintenance_lease,
    list_jobs,
    release_dispatch_lease,
)


def load_purge_inventory(paths: Any) -> dict[str, Any]:
    jobs = list_jobs(paths)
    jobs_by_id = {str(job["run_id"]): (job, state) for job, state in jobs}
    execution_paths = (
        project_paths(paths.config_path) if paths.config_path.is_file() else None
    )
    rows = (
        []
        if execution_paths is None
        else monitoring.load_registry_rows(execution_paths)
    )
    current: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for row in rows:
        if row.get("registry_kind") != "current":
            continue
        assert execution_paths is not None
        run_id = str(row["run_id"])
        current[run_id] = load_current_run(execution_paths, run_id)
    return {
        "all_jobs": jobs_by_id,
        "all_current": current,
        "execution_paths": execution_paths,
        "rows": rows,
    }


def _path_overlap(first: str, second: str) -> bool:
    left = PurePosixPath(first)
    right = PurePosixPath(second)
    return left == right or left in right.parents or right in left.parents


def output_overlap_blockers(
    records: list[dict[str, Any]],
    jobs_by_id: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    current: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    selected_ids = {str(record["run_id"]) for record in records}
    selected: list[tuple[str, str, str, str]] = []
    for record in records:
        manifest = record["manifest"]
        if manifest is not None and manifest.get("output_path") is not None:
            selected.append(
                (
                    str(manifest["server"]),
                    str(manifest["ssh"]),
                    str(manifest["output_path"]),
                    str(record["run_id"]),
                )
            )

    retained: list[tuple[str, str, str, str]] = []
    for run_id, (manifest, _state) in current.items():
        if run_id not in selected_ids and manifest.get("output_path") is not None:
            retained.append(
                (
                    str(manifest["server"]),
                    str(manifest["ssh"]),
                    str(manifest["output_path"]),
                    run_id,
                )
            )
    for run_id, (job, _state) in jobs_by_id.items():
        if run_id in selected_ids:
            continue
        raw_path = job.get("output_path")
        if raw_path is not None and len(job["prepared_servers"]) == 1:
            server = job["prepared_servers"][0]
            retained.append(
                (
                    str(server["name"]),
                    str(server["ssh"]),
                    str(raw_path),
                    run_id,
                )
            )
        raw_relpath = job.get("output_relpath")
        if raw_relpath is not None:
            for server in job["prepared_servers"]:
                root = server.get("output_root")
                if root is not None:
                    retained.append(
                        (
                            str(server["name"]),
                            str(server["ssh"]),
                            str(PurePosixPath(str(root)) / str(raw_relpath)),
                            run_id,
                        )
                    )

    blockers: list[dict[str, Any]] = []
    for server, ssh, output_path, run_id in selected:
        for retained_server, retained_ssh, retained_path, retained_run_id in retained:
            if (server == retained_server or ssh == retained_ssh) and _path_overlap(
                output_path, retained_path
            ):
                blockers.append(
                    {
                        "run_id": run_id,
                        "error": "output path overlaps a retained run",
                        "output_path": output_path,
                        "retained_run_id": retained_run_id,
                        "retained_output_path": retained_path,
                    }
                )
    return blockers


def public_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public: list[dict[str, Any]] = []
    for record in records:
        job = record["job"]
        queue_state = record["queue_state"]
        manifest = record["manifest"]
        run_state = record["run_state"]
        public.append(
            {
                "run_id": record["run_id"],
                "label": (
                    manifest.get("label")
                    if manifest is not None
                    else None
                    if job is None
                    else job.get("label")
                ),
                "queue_status": None if queue_state is None else queue_state["status"],
                "run_status": None if run_state is None else run_state["status"],
                "server": None if manifest is None else manifest.get("server"),
                "runtime": manifest is not None,
                "output_path": (
                    None if manifest is None else manifest.get("output_path")
                ),
            }
        )
    return public


def purge_execution_resources(
    paths: Any,
    records: list[dict[str, Any]],
    progress: dict[str, Any],
    *,
    owner: str,
    allowed_statuses: frozenset[str],
    timeout: int,
    remote_purge: Callable[..., dict[str, Any]],
    persist: Callable[[], None],
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for record in records:
        manifest = record["manifest"]
        run_state = record["run_state"]
        run_id = str(record["run_id"])
        if manifest is None or progress.get(run_id, {}).get("status") == "complete":
            continue
        if run_state is None or run_state["status"] not in allowed_statuses:
            failures.append(
                {"run_id": run_id, "error": "execution is not eligible for purge"}
            )
            continue
        server = str(manifest["server"])
        leased = acquire_maintenance_lease(
            paths,
            server=server,
            run_id=owner,
            ttl_seconds=600,
        )
        if not leased:
            failures.append(
                {
                    "run_id": run_id,
                    "error": f"server {server} lease is busy",
                }
            )
            continue
        try:
            latest = load_purge_inventory(paths)
            blockers = output_overlap_blockers(
                records,
                latest["all_jobs"],
                latest["all_current"],
            )
            if blockers:
                failures.append(
                    {
                        "run_id": run_id,
                        "error": (
                            "output ownership changed after the purge plan was frozen"
                        ),
                    }
                )
                continue
            result = remote_purge(
                ssh=str(manifest["ssh"]),
                python=str(manifest["project_python"]),
                run_id=run_id,
                expected_state=str(run_state["status"]),
                remote_workdir=str(manifest["remote_workdir"]),
                output_root=manifest.get("output_root"),
                output_path=manifest.get("output_path"),
                timeout=timeout,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append({"run_id": run_id, "error": str(exc)})
            continue
        finally:
            release_dispatch_lease(paths, server=server, run_id=owner)
        progress[run_id] = {"status": "complete", "result": result}
        persist()
    return failures


def _prepared_server(record: dict[str, Any]) -> dict[str, Any] | None:
    job = record["job"]
    manifest = record["manifest"]
    if job is None or manifest is None:
        return None
    for server in job["prepared_servers"]:
        if server["name"] == manifest["server"] and server["ssh"] == manifest["ssh"]:
            return server
    return None


def worktree_candidates(
    records: list[dict[str, Any]],
    inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    selected_ids = {str(record["run_id"]) for record in records}
    retained_server_workdirs = {
        (str(manifest["server"]), str(manifest["remote_workdir"]))
        for run_id, (manifest, _state) in inventory["all_current"].items()
        if run_id not in selected_ids
    }
    retained_ssh_workdirs = {
        (str(manifest["ssh"]), str(manifest["remote_workdir"]))
        for run_id, (manifest, _state) in inventory["all_current"].items()
        if run_id not in selected_ids
    }
    for run_id, (job, _state) in inventory["all_jobs"].items():
        if run_id in selected_ids:
            continue
        revision = str(job["revision"])
        for server in job["prepared_servers"]:
            workdir = str(PurePosixPath(str(server["worktree_root"])) / revision)
            retained_server_workdirs.add((str(server["name"]), workdir))
            retained_ssh_workdirs.add((str(server["ssh"]), workdir))

    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        manifest = record["manifest"]
        server = _prepared_server(record)
        if manifest is None or server is None:
            continue
        revision = manifest.get("source_revision") or record["job"].get("revision")
        if not isinstance(revision, str):
            continue
        candidate = {
            "server": str(server["name"]),
            "ssh": str(server["ssh"]),
            "python": str(server["python"]),
            "bare_repo": str(server["bare_repo"]),
            "worktree_root": str(server["worktree_root"]),
            "remote_workdir": str(manifest["remote_workdir"]),
            "revision": revision,
        }
        server_key = (candidate["server"], candidate["remote_workdir"])
        ssh_key = (candidate["ssh"], candidate["remote_workdir"])
        if (
            server_key not in retained_server_workdirs
            and ssh_key not in retained_ssh_workdirs
        ):
            candidates[ssh_key] = candidate
    return list(candidates.values())


def purge_worktrees(
    paths: Any,
    records: list[dict[str, Any]],
    progress: dict[str, Any],
    inventory: dict[str, Any],
    *,
    owner: str,
    timeout: int,
    remote_purge: Callable[..., dict[str, Any]],
    persist: Callable[[], None],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    failures: list[dict[str, str]] = []
    preserved: list[dict[str, Any]] = []
    candidates = worktree_candidates(records, inventory)
    candidate_keys = {f"{item['ssh']}:{item['remote_workdir']}" for item in candidates}
    for record in records:
        manifest = record["manifest"]
        if manifest is None:
            continue
        key = f"{manifest['ssh']}:{manifest['remote_workdir']}"
        if key not in candidate_keys:
            preserved.append(
                {
                    "ssh": manifest["ssh"],
                    "remote_workdir": manifest["remote_workdir"],
                    "reason": "shared or ownership could not be proven",
                }
            )
    for candidate in candidates:
        key = f"{candidate['ssh']}:{candidate['remote_workdir']}"
        if progress.get(key, {}).get("status") == "complete":
            continue
        leased = acquire_maintenance_lease(
            paths,
            server=str(candidate["server"]),
            run_id=owner,
            ttl_seconds=600,
        )
        if not leased:
            failures.append(
                {
                    "run_id": owner,
                    "error": f"server {candidate['server']} lease is busy",
                }
            )
            continue
        try:
            result = remote_purge(
                ssh=str(candidate["ssh"]),
                python=str(candidate["python"]),
                bare_repo=str(candidate["bare_repo"]),
                worktree_root=str(candidate["worktree_root"]),
                remote_workdir=str(candidate["remote_workdir"]),
                revision=str(candidate["revision"]),
                timeout=timeout,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            failures.append({"run_id": owner, "error": str(exc)})
        else:
            progress[key] = {"status": "complete", "result": result}
            persist()
        finally:
            release_dispatch_lease(
                paths,
                server=str(candidate["server"]),
                run_id=owner,
            )
    return failures, preserved
