from __future__ import annotations

import argparse
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config
from .pool import probe_project_pool
from .source import (
    DeploymentTarget,
    HistoricalSourceSelection,
    prepare_revision,
    select_historical_source_repo,
)
from .submission import (
    prepared_server_manifest,
    reachable_targets,
    resolve_source_repo,
)


def _pending_jobs(value: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = value.get("jobs")
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise RuntimeError("controller returned invalid pending all-server jobs")
    return jobs


def sync(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    pending = _pending_jobs(
        call_controller(config, "pending-all", timeout=args.timeout)
    )
    pending_revisions = tuple(str(job.get("revision")) for job in pending)
    source: HistoricalSourceSelection | None = None
    source_failure: str | None = None
    prepared_cache: dict[tuple[str, str], dict[str, Any]] = {}
    failed_cache: dict[tuple[str, str], str] = {}
    policy_failures: list[dict[str, str]] = []
    pool_cache: dict[tuple[int, tuple[str, ...]], list[dict[str, Any]]] = {}
    updates: list[dict[str, Any]] = []

    for job in pending:
        run_id = str(job.get("run_id"))
        revision = str(job.get("revision"))
        minimum_cores = job.get("minimum_cores")
        workload_class = job.get("workload_class")
        existing_raw = job.get("prepared_servers")
        if (
            isinstance(minimum_cores, bool)
            or not isinstance(minimum_cores, int)
            or minimum_cores <= 0
            or workload_class not in {"standard", "test"}
            or not isinstance(existing_raw, list)
            or any(not isinstance(name, str) for name in existing_raw)
        ):
            raise RuntimeError(f"controller returned invalid pool constraints for {run_id}")
        testing_servers = (
            config.scheduling.testing_servers if workload_class == "test" else ()
        )
        pool_key = (minimum_cores, testing_servers)
        if pool_key not in pool_cache:
            pool_cache[pool_key] = probe_project_pool(
                config,
                args.server_registry.expanduser(),
                explicit_server=None,
                ssh_profile=args.ssh_profile,
                timeout=args.timeout,
                minimum_cores=minimum_cores,
                candidate_servers=testing_servers or None,
            )
        existing = set(existing_raw)
        missing_pool = [
            candidate
            for candidate in pool_cache[pool_key]
            if candidate["name"] not in existing
        ]
        if not missing_pool:
            continue
        for candidate in missing_pool:
            probe = candidate.get("probe")
            if not isinstance(probe, dict) or probe.get("reachable") is not True:
                failed_cache.setdefault(
                    (revision, str(candidate["name"])),
                    str(
                        probe.get("error", "unreachable")
                        if isinstance(probe, dict)
                        else "unreachable"
                    ),
                )
        try:
            targets, candidates = reachable_targets(
                missing_pool,
                explicit_server=None,
            )
        except RuntimeError:
            continue

        additions: list[dict[str, Any]] = []
        for target in targets:
            key = (revision, target.name)
            candidate = candidates[target.name]
            if job.get("output_relpath") is not None and candidate["runtime"].get(
                "output_root"
            ) is None:
                policy_failures.append(
                    {
                        "run_id": run_id,
                        "revision": revision,
                        "server": target.name,
                        "error": "relative output requires configured output_root",
                    }
                )
                continue
            if workload_class == "test" and int(candidate.get("test_slots", 0)) <= 0:
                policy_failures.append(
                    {
                        "run_id": run_id,
                        "revision": revision,
                        "server": target.name,
                        "error": "test workload requires positive testing.slots",
                    }
                )
                continue
            if key in prepared_cache:
                additions.append(prepared_cache[key])
                continue
            if key in failed_cache:
                continue
            if source is None and source_failure is None:
                source_override = getattr(args, "source_repo", None)
                try:
                    source = select_historical_source_repo(
                        config.local_repo,
                        (
                            resolve_source_repo(config.local_repo, source_override)
                            if source_override is not None
                            else None
                        ),
                        revisions=pending_revisions,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    source_failure = str(exc)
            if source_failure is not None:
                failed_cache[key] = source_failure
                continue
            if source is None:
                raise AssertionError("historical source selection returned no result")
            try:
                preparation = prepare_revision(
                    source.source_repo,
                    project_id=config.project_id,
                    targets=[
                        DeploymentTarget(
                            name=target.name,
                            remote_url=target.remote_url,
                        )
                    ],
                    explicit_server=target.name,
                    revision=revision,
                    timeout=args.prepare_timeout,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                failed_cache[key] = str(exc)
                continue
            descriptor = prepared_server_manifest(preparation, candidates)[0]
            prepared_cache[key] = descriptor
            additions.append(descriptor)
        if additions:
            updates.append(
                {
                    "run_id": run_id,
                    "revision": revision,
                    "prepared_servers": additions,
                }
            )

    controller = (
        call_controller(
            config,
            "extend-all",
            timeout=args.timeout,
            payload={"updates": updates},
        )
        if updates
        else {"results": [], "extended_count": 0, "dispatcher_started": False}
    )
    failures = policy_failures + [
        {"revision": revision, "server": server, "error": error}
        for (revision, server), error in sorted(failed_cache.items())
    ]
    return {
        "pending_count": len(pending),
        "update_count": len(updates),
        "prepared_count": len(prepared_cache),
        "preparation_failures": failures,
        "source": source.audit() if source is not None else None,
        "controller": controller,
    }
