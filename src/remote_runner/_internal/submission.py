from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path, PurePosixPath
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config, sha256_bytes
from .experiment_contracts import (
    MAX_CONTRACT_BYTES,
    normalize_run_binding,
)
from .output_paths import normalize_output_relpath
from .pool import (
    normalize_candidate_servers,
    normalize_explicit_server,
    probe_project_pool,
)
from .preparation_manifest import load_preparation_manifest
from .result_metadata import normalize_result_intent, parse_result_tags
from .scheduling import (
    default_worker_policy,
    normalize_minimum_cores,
    normalize_worker_policy,
    normalize_workload_class,
)
from .source import DeploymentTarget, PreparationResult, prepare_revision


def resolve_source_repo(config_repo: Path, override: Path | None) -> Path:
    if override is None:
        return config_repo
    if not override.is_absolute():
        raise ValueError("--source-repo must be an absolute path")
    return override.expanduser().resolve()


def _remote_url(ssh: str, bare_repo: str) -> str:
    if not ssh or ":" in ssh:
        raise ValueError("selected SSH target must be a host alias or user@host")
    if not PurePosixPath(bare_repo).is_absolute():
        raise ValueError("remote bare repository must be an absolute POSIX path")
    return f"{ssh}:{bare_repo}"


def _reachable_targets(
    pool: list[dict[str, Any]],
    *,
    explicit_server: str | None,
) -> tuple[list[DeploymentTarget], dict[str, Any]]:
    targets: list[DeploymentTarget] = []
    candidates: dict[str, Any] = {}
    for raw in pool:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            continue
        name = str(raw["name"])
        candidates[name] = raw
        probe = raw.get("probe")
        runtime = raw.get("runtime")
        if not isinstance(probe, dict) or probe.get("reachable") is not True:
            continue
        if not isinstance(runtime, dict):
            continue
        targets.append(
            DeploymentTarget(
                name=name,
                remote_url=_remote_url(str(raw["ssh"]), str(runtime["bare_repo"])),
            )
        )
    if not targets:
        if explicit_server is not None:
            raise RuntimeError(f"explicit server {explicit_server!r} is unreachable")
        raise RuntimeError("no reachable configured server can receive the source revision")
    return targets, candidates


def _prepared_manifest(
    preparation: PreparationResult,
    candidates: dict[str, Any],
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for item in preparation.prepared:
        candidate = candidates[item.name]
        runtime = candidate["runtime"]
        prepared.append(
            {
                "name": item.name,
                "ssh": candidate["ssh"],
                "ssh_profile": candidate["ssh_profile"],
                "configured_cores": candidate["cores"],
                "priority": candidate["priority"],
                "bare_repo": runtime["bare_repo"],
                "worktree_root": runtime["worktree_root"],
                "python": runtime["python"],
                "output_root": runtime.get("output_root"),
                "test_slots": candidate.get("test_slots", 0),
            }
        )
    return prepared


def _eligible_prepared_servers(
    prepared_servers: list[dict[str, Any]],
    *,
    minimum_cores: int,
    candidate_servers: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    required = normalize_minimum_cores(minimum_cores)
    allowed = None if candidate_servers is None else set(candidate_servers)
    eligible: list[dict[str, Any]] = []
    for server in prepared_servers:
        if allowed is not None and server.get("name") not in allowed:
            continue
        cores = server.get("configured_cores")
        if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
            raise ValueError(
                "prepared server configured_cores must be a positive integer"
            )
        if cores >= required:
            normalized = dict(server)
            normalized["output_root"] = server.get("output_root")
            normalized["test_slots"] = server.get("test_slots", 0)
            eligible.append(normalized)
    if not eligible:
        if candidate_servers is not None:
            names = ", ".join(candidate_servers)
            raise ValueError(
                "no allowed candidate server is in the eligible prepared server set: "
                f"{names}"
            )
        raise ValueError(f"no prepared server has at least {required} configured cores")
    return eligible


def _requested_output(args: argparse.Namespace) -> str | None:
    raw_relpath = getattr(args, "output_relpath", None)
    return (
        None if raw_relpath is None else normalize_output_relpath(raw_relpath)
    )


def _validate_output_candidates(
    prepared_servers: list[dict[str, Any]],
    *,
    output_relpath: str | None,
) -> None:
    if output_relpath is not None:
        missing = sorted(
            str(server.get("name"))
            for server in prepared_servers
            if server.get("output_root") is None
        )
        if missing:
            raise ValueError(
                "--output-relpath requires remote output_root for every eligible "
                f"server; missing: {', '.join(missing)}"
            )


def _finalize_experiment_binding(
    path: Path | None,
    *,
    run_id: str,
    source_revision: str,
) -> dict[str, Any] | None:
    if path is None:
        return None
    expanded = path.expanduser()
    resolved = expanded.resolve()
    if expanded.is_symlink() or not resolved.is_file():
        raise ValueError(f"--experiment-binding must be a regular JSON file: {path}")
    if resolved.stat().st_size > MAX_CONTRACT_BYTES:
        raise ValueError(
            f"--experiment-binding exceeds the {MAX_CONTRACT_BYTES}-byte limit"
        )
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"--experiment-binding must contain UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("--experiment-binding must contain one JSON object")
    binding = dict(value)
    for field, expected in (
        ("run_id", run_id),
        ("source_revision", source_revision),
    ):
        supplied = binding.get(field)
        if supplied is not None and supplied != expected:
            raise ValueError(f"experiment binding {field} does not match this run")
        binding[field] = expected
    supplied_id = binding.get("binding_id")
    if supplied_id is None:
        binding["binding_id"] = f"binding-{secrets.token_hex(8)}"
    return normalize_run_binding(binding)


def submit(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    output_relpath = _requested_output(args)
    source_repo = resolve_source_repo(config.local_repo, args.source_repo)
    minimum_cores = normalize_minimum_cores(getattr(args, "minimum_cores", 1))
    workload_class = normalize_workload_class(
        getattr(args, "workload_class", "standard")
    )
    raw_worker_policy = getattr(args, "worker_policy", None)
    worker_policy = normalize_worker_policy(
        default_worker_policy(workload_class)
        if raw_worker_policy is None
        else raw_worker_policy
    )
    result_intent = normalize_result_intent(
        getattr(args, "result_intent", "candidate"),
        field="--result-intent",
    )
    result_tags = parse_result_tags(getattr(args, "result_tags", None))
    raw_server = getattr(args, "server", None)
    requested_server = normalize_explicit_server(raw_server)
    server_scope = "all" if raw_server == "all" else "snapshot"
    candidate_servers = normalize_candidate_servers(
        getattr(args, "candidate_servers", None)
    )
    if candidate_servers is not None and minimum_cores != 1:
        raise ValueError(
            "--candidate-server cannot be combined with a non-default --min-cores"
        )
    if candidate_servers is not None:
        config.candidate_names(candidate_servers=candidate_servers)
    if workload_class == "test":
        if requested_server is not None:
            raise ValueError(
                "--server cannot be combined with --workload-class test; use "
                "--candidate-server to select a testing-pool subset"
            )
        testing_servers = config.scheduling.testing_servers
        if not testing_servers:
            raise ValueError(
                "--workload-class test requires project config scheduling.testing.servers"
            )
        if candidate_servers is not None:
            testing_names = set(testing_servers)
            outside_testing_pool = [
                name for name in candidate_servers if name not in testing_names
            ]
            if outside_testing_pool:
                raise ValueError(
                    "--candidate-server for a test workload must be a subset of "
                    "scheduling.testing.servers; outside testing pool: "
                    f"{', '.join(outside_testing_pool)}"
                )
    else:
        testing_servers = ()
    prepared_manifest_path = getattr(args, "prepared_manifest", None)
    if prepared_manifest_path is None:
        pool = probe_project_pool(
            config,
            args.server_registry.expanduser(),
            explicit_server=requested_server,
            ssh_profile=args.ssh_profile,
            timeout=args.timeout,
            minimum_cores=minimum_cores,
            candidate_servers=candidate_servers or testing_servers or None,
        )
        targets, candidates = _reachable_targets(pool, explicit_server=requested_server)
        preparation = prepare_revision(
            source_repo,
            project_id=config.project_id,
            targets=targets,
            explicit_server=requested_server,
            timeout=args.prepare_timeout,
        )
        revision = preparation.revision
        prepared_servers = _eligible_prepared_servers(
            _prepared_manifest(preparation, candidates),
            minimum_cores=minimum_cores,
            candidate_servers=candidate_servers,
        )
        preparation_failures = [item.__dict__ for item in preparation.failures]
        preparation_reused = False
    else:
        if requested_server is not None:
            raise ValueError("--prepared-manifest cannot be combined with --server")
        prepared = load_preparation_manifest(
            prepared_manifest_path,
            config=config,
            server_registry_path=args.server_registry,
            source_repo=source_repo,
        )
        revision = str(prepared["revision"])
        prepared_servers = _eligible_prepared_servers(
            list(prepared["prepared_servers"]),
            minimum_cores=minimum_cores,
            candidate_servers=candidate_servers,
        )
        preparation_failures = list(prepared["preparation_failures"])
        preparation_reused = True
    if workload_class == "test":
        testing_names = set(testing_servers)
        prepared_servers = [
            server for server in prepared_servers if server["name"] in testing_names
        ]
        if not prepared_servers:
            raise ValueError("no configured testing server is in the prepared server set")
        missing_slots = sorted(
            str(server["name"])
            for server in prepared_servers
            if int(server.get("test_slots", 0)) <= 0
        )
        if missing_slots:
            raise ValueError(
                "testing servers must configure positive testing.slots in the global "
                f"server registry; missing: {', '.join(missing_slots)}"
            )
    _validate_output_candidates(
        prepared_servers,
        output_relpath=output_relpath,
    )
    run_id = args.run_id or f"rr-{secrets.token_hex(8)}"
    experiment_binding = _finalize_experiment_binding(
        getattr(args, "experiment_binding", None),
        run_id=run_id,
        source_revision=revision,
    )
    if experiment_binding is not None and experiment_binding["expects_result_manifest"]:
        if output_relpath is None:
            raise ValueError(
                "a result-producing experiment binding requires --output-relpath"
            )
        if config.output_sync is None:
            raise ValueError(
                "a result-producing experiment binding requires configured output_sync"
            )
        if result_intent != "candidate":
            raise ValueError(
                "a result-producing experiment binding requires --result-intent candidate"
            )
    command = args.command
    job = {
        "run_id": run_id,
        "revision": revision,
        "label": args.label,
        "task_id": args.task_id,
        "result_intent": result_intent,
        "result_tags": result_tags,
        "queue_priority": args.queue_priority,
        "workload_class": workload_class,
        "worker_policy": worker_policy,
        "submitted_command": command,
        "submitted_command_sha256": sha256_bytes(command.encode("utf-8")),
        "worker_arg": config.parallelism.default_arg,
        "minimum_cores": minimum_cores,
        "server_scope": server_scope,
        "prepared_servers": prepared_servers,
        "output_relpath": output_relpath,
        "output_path": None,
        "output_metadata": json.loads(args.output_metadata) if args.output_metadata else {},
        "output_sync": (
            None if config.output_sync is None else config.output_sync.to_payload()
        ),
        "lease_seconds": config.scheduling.lease_seconds,
        "privacy": args.privacy,
        "experiment_binding": experiment_binding,
    }
    controller = call_controller(
        config,
        "submit",
        timeout=args.timeout,
        payload=job,
    )
    return {
        "run_id": run_id,
        "revision": revision,
        "prepared_servers": [item["name"] for item in prepared_servers],
        "minimum_cores": minimum_cores,
        "server_scope": server_scope,
        "workload_class": workload_class,
        "worker_policy": worker_policy,
        "result_intent": result_intent,
        "result_tags": result_tags,
        "experiment_binding": (
            None
            if experiment_binding is None
            else {
                "binding_id": experiment_binding["binding_id"],
                "binding_digest": experiment_binding["binding_digest"],
            }
        ),
        "preparation_failures": preparation_failures,
        "preparation_reused": preparation_reused,
        "controller": controller,
    }
