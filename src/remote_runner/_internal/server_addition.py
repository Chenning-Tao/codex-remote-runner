from __future__ import annotations

import argparse
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config, validate_current_run_id
from .pool import probe_project_pool
from .source import DeploymentTarget, prepare_revision
from .submission import (
    _prepared_manifest,
    _reachable_targets,
    resolve_source_repo,
)


def _queued_job(value: dict[str, Any], run_id: str) -> dict[str, Any]:
    job = value.get("job")
    if not isinstance(job, dict) or job.get("run_id") != run_id:
        raise RuntimeError("controller returned an invalid queued job")
    revision = job.get("revision")
    minimum_cores = job.get("minimum_cores")
    workload_class = job.get("workload_class")
    prepared_servers = job.get("prepared_servers")
    if (
        not isinstance(revision, str)
        or isinstance(minimum_cores, bool)
        or not isinstance(minimum_cores, int)
        or minimum_cores <= 0
        or workload_class not in {"standard", "test"}
        or not isinstance(prepared_servers, list)
        or not prepared_servers
        or any(not isinstance(name, str) for name in prepared_servers)
    ):
        raise RuntimeError("controller returned invalid queued job constraints")
    return job


def add(args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_current_run_id(args.run_id)
    if args.server == "all":
        raise ValueError("--server must name one configured server, not 'all'")

    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    queued = _queued_job(
        call_controller(
            config,
            "queued-job",
            timeout=args.timeout,
            action_args=("--run-id", run_id),
        ),
        run_id,
    )
    revision = str(queued["revision"])
    existing = [str(name) for name in queued["prepared_servers"]]
    if args.server in existing:
        return {
            "run_id": run_id,
            "revision": revision,
            "requested_server": args.server,
            "prepared_servers": existing,
            "outcome": {"action": "unchanged", "reason": "server already allowed"},
        }
    if queued.get("output_path") is not None:
        raise ValueError(
            "cannot add a server to a historical queued run with an absolute "
            "output identity; resubmit with --output-relpath"
        )
    if (
        queued["workload_class"] == "test"
        and args.server not in config.scheduling.testing_servers
    ):
        raise ValueError(
            f"test workload server {args.server!r} is not in scheduling.testing.servers"
        )

    pool = probe_project_pool(
        config,
        args.server_registry.expanduser(),
        explicit_server=args.server,
        ssh_profile=args.ssh_profile,
        timeout=args.timeout,
        minimum_cores=int(queued["minimum_cores"]),
    )
    targets, candidates = _reachable_targets(pool, explicit_server=args.server)
    candidate = candidates[args.server]
    if (
        queued.get("output_relpath") is not None
        and candidate["runtime"].get("output_root") is None
    ):
        raise ValueError(
            f"server {args.server!r} requires output_root for this relative-output run"
        )
    if queued["workload_class"] == "test" and int(candidate.get("test_slots", 0)) <= 0:
        raise ValueError(
            f"test workload server {args.server!r} requires positive testing.slots"
        )

    source_repo = resolve_source_repo(config.local_repo, args.source_repo)
    target = targets[0]
    preparation = prepare_revision(
        source_repo,
        project_id=config.project_id,
        targets=[
            DeploymentTarget(
                name=target.name,
                remote_url=target.remote_url,
            )
        ],
        explicit_server=args.server,
        revision=revision,
        timeout=args.prepare_timeout,
    )
    descriptor = _prepared_manifest(preparation, candidates)[0]
    controller = call_controller(
        config,
        "extend-job",
        timeout=args.timeout,
        action_args=("--run-id", run_id),
        payload={
            "revision": revision,
            "prepared_servers": [descriptor],
            **(
                {"placement_token": args.placement_token}
                if getattr(args, "placement_token", None) is not None
                else {}
            ),
        },
    )
    status = controller.get("status")
    added_servers = controller.get("added_servers")
    prepared_servers = controller.get("prepared_servers")
    if (
        status not in {"extended", "unchanged"}
        or isinstance(added_servers, bool)
        or not isinstance(added_servers, int)
        or not isinstance(prepared_servers, list)
        or any(not isinstance(name, str) for name in prepared_servers)
    ):
        raise RuntimeError("controller returned an invalid queued job extension")
    return {
        "run_id": run_id,
        "revision": revision,
        "requested_server": args.server,
        "prepared_servers": prepared_servers,
        "outcome": {
            "action": status,
            "added_servers": added_servers,
        },
        "controller": controller,
    }
