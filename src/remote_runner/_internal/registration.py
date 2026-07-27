from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
from typing import Any

from .execution_registry import (
    CURRENT_MANIFEST_SCHEMA,
    CURRENT_STATE_SCHEMA,
    PROCESS_TITLE_PRIVACY_MODE,
    generate_run_id,
    project_paths,
    register_current_run,
    resolve_project_config,
    sha256_bytes,
    utc_now,
    validate_current_run_id,
)
from .experiment_contracts import normalize_run_binding
from .output_paths import validate_resolved_output
from .result_metadata import (
    LEGACY_RESULT_INTENT,
    normalize_result_intent,
    normalize_result_tags,
)
from .scheduling import normalize_minimum_cores, normalize_workload_class


def _output_metadata(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--output-metadata must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("--output-metadata must decode to an object")
    return value


def register(args: argparse.Namespace) -> Path:
    config_path = resolve_project_config(args.project_config)
    paths = project_paths(config_path)
    remote_workdir = getattr(args, "remote_workdir", None)
    project_python = getattr(args, "project_python", None)
    if not isinstance(remote_workdir, str) or not isinstance(project_python, str):
        raise ValueError("controller registration requires remote_workdir and project_python")
    for field, value in (
        ("remote_workdir", remote_workdir),
        ("project_python", project_python),
    ):
        if not value or "\n" in value or "\x00" in value:
            raise ValueError(f"{field} must be a non-empty path")
        if not PurePosixPath(value).is_absolute():
            raise ValueError(f"{field} must be an absolute POSIX path")
    runtime = {"workdir": remote_workdir, "python": project_python}
    if args.configured_cores <= 0:
        raise ValueError("--configured-cores must be positive")
    minimum_cores = normalize_minimum_cores(getattr(args, "minimum_cores", 1))
    workload_class = normalize_workload_class(
        getattr(args, "workload_class", "standard")
    )
    raw_result_intent = getattr(args, "result_intent", None)
    result_intent = normalize_result_intent(
        LEGACY_RESULT_INTENT if raw_result_intent is None else raw_result_intent,
        allow_unclassified=raw_result_intent is None,
        field="result_intent",
    )
    result_tags = normalize_result_tags(
        getattr(args, "result_tags", {}), field="result_tags"
    )
    if args.configured_cores < minimum_cores:
        raise ValueError("selected server does not satisfy minimum cores")
    if args.workers is not None and args.workers <= 0:
        raise ValueError("--workers must be positive when supplied")
    if "\x00" in args.command or not args.command.strip():
        raise ValueError(
            "--command must be non-empty UTF-8 shell text without NUL bytes"
        )
    output_root, output_relpath, output_path = validate_resolved_output(
        output_root=getattr(args, "output_root", None),
        output_relpath=getattr(args, "output_relpath", None),
        output_path=getattr(args, "output_path", None),
    )

    run_id = args.run_id or generate_run_id(runs_dir=paths.runs_dir)
    validate_current_run_id(run_id)
    command_bytes = args.command.encode("utf-8")
    now = utc_now()
    manifest: dict[str, Any] = {
        "schema_version": CURRENT_MANIFEST_SCHEMA,
        "run_id": run_id,
        "label": args.label,
        "task_id": args.task_id,
        "result_intent": result_intent,
        "result_tags": result_tags,
        "workload_class": workload_class,
        "project_root": str(paths.project_root),
        "project_config": str(paths.config_path),
        "registry_root": str(paths.registry_root),
        "server": args.server,
        "ssh": args.ssh,
        "ssh_profile": args.ssh_profile,
        "configured_cores": args.configured_cores,
        "minimum_cores": minimum_cores,
        "workers": args.workers,
        "remote_workdir": runtime["workdir"],
        "project_python": runtime["python"],
        "expected_revision": args.expected_revision,
        "require_clean_worktree": args.require_clean_worktree,
        "output_root": output_root,
        "output_relpath": output_relpath,
        "output_path": output_path,
        "output_metadata": _output_metadata(args.output_metadata),
        "command": args.command,
        "command_path": "command.sh",
        "command_sha256": sha256_bytes(command_bytes),
        "created_at": now,
    }
    source_revision = getattr(args, "source_revision", None)
    prepared_servers = getattr(args, "prepared_servers", None)
    submitted_command = getattr(args, "submitted_command", None)
    worker_defaulted = getattr(args, "worker_defaulted", False)
    if source_revision is not None:
        manifest["source_revision"] = source_revision
    if prepared_servers is not None:
        manifest["prepared_servers"] = list(prepared_servers)
    if submitted_command is not None:
        manifest["submitted_command"] = submitted_command
        manifest["submitted_command_sha256"] = sha256_bytes(submitted_command.encode("utf-8"))
    manifest["worker_defaulted"] = worker_defaulted is True
    raw_experiment_binding = getattr(args, "experiment_binding", None)
    if raw_experiment_binding is not None:
        experiment_binding = normalize_run_binding(raw_experiment_binding)
        if experiment_binding["run_id"] != run_id:
            raise ValueError("experiment binding run_id does not match the manifest")
        if source_revision is None or experiment_binding["source_revision"] != source_revision:
            raise ValueError(
                "experiment binding source_revision does not match the manifest"
            )
        manifest["experiment_binding"] = experiment_binding
    privacy = getattr(args, "privacy", None)
    if privacy is not None:
        if privacy != PROCESS_TITLE_PRIVACY_MODE:
            raise ValueError(f"unsupported privacy mode: {privacy!r}")
        manifest["process_title_privacy"] = {"mode": "required"}
    state = {
        "state_schema_version": CURRENT_STATE_SCHEMA,
        "run_id": run_id,
        "revision": 0,
        "status": "registered",
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "error": None,
    }
    return register_current_run(paths, manifest, state, command_bytes)
