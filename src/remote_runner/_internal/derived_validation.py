"""Client orchestration for `remote-runner validate-run`.

One exact source run derives exactly one durable validator run. This module owns
the caller-facing sequence — look up the frozen identity, prepare the source
revision on the archive target, submit or resume the validator, wait for it to be
reportable, and read back one small project result file under guard.

It never interprets that result. `passed`, `failed`, and everything else inside
the payload belong to the project that produced them; Remote Runner only reports
that a lifecycle completed and that bytes were transported intact.
"""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from . import derived_result_remote, waiting
from .config import ManagedProjectConfig, load_managed_project_config
from .controller.client import call_controller
from .derivation import (
    build_relation,
    canonical_bytes,
    normalize_artifact_path,
    normalize_result_relpath,
    normalize_validator_key,
    relation_identity,
    spec_digest,
    validate_relation,
)
from .execution_registry import resolve_project_config, sha256_bytes, validate_current_run_id
from .pool import probe_project_pool
from .remote_shell import remote_python_stdin_command, ssh_connection_options
from .scheduling import normalize_requested_cores
from .source import (
    prepare_revision,
    resolve_source_repo,
    select_historical_source_repo,
)
from .submission import (
    filter_eligible_prepared_servers,
    prepared_server_manifest,
    reachable_targets,
)


RESULT_SCHEMA = "remote-runner-derived-validation/v1"
VALIDATOR_WORKLOAD_CLASS = "test"
CHECKSUM_VERIFICATION = "rsync_checksum_dry_run"
_RESULT_READ_BUDGET_SECONDS = 60
_EXIT_CODES = {
    "validated": 0,
    "submitted": 0,
    "result_unavailable": 1,
    "invalid_request": 2,
    "validation_pending": 3,
    "validator_failed": 4,
    "attention_required": 4,
}


class ResultUnavailable(RuntimeError):
    """The validator is reportable but its result could not be read safely."""


def validation_exit_code(result: dict[str, Any]) -> int:
    """Map one derived-validation result to its exit code.

    0 succeeded, 1 result retrieval failed, 2 invalid request, 3 observation
    window expired, 4 validator failure or attention — the last three match the
    codes `remote-runner wait` already uses.
    """
    status = str(result.get("status"))
    if status not in _EXIT_CODES:
        raise ValueError(f"unsupported derived validation status: {status!r}")
    return _EXIT_CODES[status]


def _result(
    *,
    status: str,
    source: dict[str, Any] | None = None,
    validator: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    wait: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA,
        "status": status,
        "source": source,
        "validator": validator,
        "result": result,
        "wait": wait,
        "error": error,
    }


def _call(
    config: ManagedProjectConfig,
    action: str,
    *,
    timeout: int,
    action_args: tuple[str, ...] = (),
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return call_controller(
            config,
            action,
            timeout=timeout,
            action_args=action_args,
            payload=payload,
        )
    except RuntimeError as exc:
        detail = str(exc)
        if "invalid choice" in detail and action in detail:
            raise RuntimeError(
                f"the active controller runtime does not support the {action!r} "
                "action; activate a controller release that matches this client "
                "before deriving validation runs"
            ) from exc
        raise


def _validator_label(validator_key: str) -> str:
    return f"validate:{validator_key}"


def _validator_task_id(source_run_id: str) -> str:
    return f"validation/{source_run_id}"


def _validator_output_relpath(validator_run_id: str) -> str:
    return f"validation/{validator_run_id}"


def validator_spec_sha256(
    relation: dict[str, Any],
    *,
    validator_run_id: str,
    command: str,
    requested_cores: int,
    privacy: str | None,
) -> str:
    """Digest the immutable spec one validator submission freezes."""
    return spec_digest(
        relation,
        label=_validator_label(relation["validator_key"]),
        task_id=_validator_task_id(relation["source_run_id"]),
        submitted_command_sha256=sha256_bytes(command.encode("utf-8")),
        minimum_cores=1,
        requested_cores=requested_cores,
        workload_class=VALIDATOR_WORKLOAD_CLASS,
        output_relpath=_validator_output_relpath(validator_run_id),
        privacy=privacy,
        eligible_servers=[relation["source_artifact"]["target_server"]],
    )


def _existing_relation(view: dict[str, Any]) -> dict[str, Any]:
    queue = view.get("queue")
    if not isinstance(queue, dict) or queue.get("derivation") is None:
        raise ValueError(
            "the existing validator run has no readable derivation relation; "
            "inspect it directly instead of deriving it again"
        )
    return validate_relation(queue["derivation"])


def _validator_facts(
    *,
    validator_run_id: str,
    relation: dict[str, Any],
    disposition: str | None,
    view: dict[str, Any] | None,
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "run_id": validator_run_id,
        "validator_key": relation["validator_key"],
        "submission_disposition": disposition,
        "workload_class": VALIDATOR_WORKLOAD_CLASS,
        "server": relation["source_artifact"]["target_server"],
        "output_relpath": _validator_output_relpath(validator_run_id),
        "derivation": relation,
    }
    if view is not None:
        facts.update(
            {
                "phase": view.get("phase"),
                "outcome": view.get("outcome"),
                "attention_reason": view.get("attention_reason"),
                "output_sync": (view.get("output_sync") or {}).get("status"),
            }
        )
    return facts


def _source_facts(relation: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": relation["source_run_id"],
        "revision": relation["source_revision"],
        "server": relation["source_server"],
        "artifact": dict(relation["source_artifact"]),
    }


def _archive_candidate(
    config: ManagedProjectConfig,
    args: argparse.Namespace,
    *,
    archive_target: str,
    requested_cores: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if archive_target not in config.scheduling.testing_servers:
        raise ValueError(
            f"archive target {archive_target!r} must also be listed in project "
            "config scheduling.testing.servers before validators can run on it"
        )
    pool = probe_project_pool(
        config,
        args.server_registry.expanduser(),
        explicit_server=archive_target,
        ssh_profile=args.ssh_profile,
        timeout=args.timeout,
        minimum_cores=requested_cores,
    )
    candidate = next(
        (item for item in pool if str(item.get("name")) == archive_target),
        None,
    )
    if candidate is None:
        raise RuntimeError(f"archive target {archive_target!r} is not a candidate server")
    probe = candidate.get("probe")
    if not isinstance(probe, dict) or probe.get("reachable") is not True:
        raise RuntimeError(f"archive target {archive_target!r} is unreachable")
    if int(candidate.get("test_slots", 0)) <= 0:
        raise ValueError(
            f"archive target {archive_target!r} must configure positive testing.slots "
            "in the global server registry"
        )
    runtime = candidate.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("output_root") is None:
        raise ValueError(
            f"archive target {archive_target!r} must configure output_root before a "
            "validator can write a portable result artifact"
        )
    return candidate, pool


def _submit(
    config: ManagedProjectConfig,
    args: argparse.Namespace,
    *,
    relation: dict[str, Any],
    validator_run_id: str,
    command: str,
    requested_cores: int,
    privacy: str | None,
    pool: list[dict[str, Any]],
) -> dict[str, Any]:
    archive_target = relation["source_artifact"]["target_server"]
    revision = relation["source_revision"]
    targets, candidates = reachable_targets(pool, explicit_server=archive_target)
    override = (
        None
        if getattr(args, "source_repo", None) is None
        else resolve_source_repo(config.local_repo, args.source_repo)
    )
    selection = select_historical_source_repo(
        config.local_repo,
        override,
        revisions=(revision,),
    )
    preparation = prepare_revision(
        selection.source_repo,
        project_id=config.project_id,
        targets=targets,
        explicit_server=archive_target,
        revision=revision,
        timeout=args.prepare_timeout,
    )
    prepared_servers = filter_eligible_prepared_servers(
        prepared_server_manifest(preparation, candidates),
        minimum_cores=1,
        requested_cores=requested_cores,
        candidate_servers=(archive_target,),
    )
    job = build_validator_job(
        relation,
        validator_run_id=validator_run_id,
        command=command,
        requested_cores=requested_cores,
        privacy=privacy,
        prepared_servers=prepared_servers,
        lease_seconds=config.scheduling.lease_seconds,
    )
    return _call(config, "submit-validation", timeout=args.timeout, payload=job)


def build_validator_job(
    relation: dict[str, Any],
    *,
    validator_run_id: str,
    command: str,
    requested_cores: int,
    privacy: str | None,
    prepared_servers: list[dict[str, Any]],
    lease_seconds: int,
) -> dict[str, Any]:
    """Build the one durable job a derived validation submits.

    Label, task identity, and output path are derived from the relation so an
    exact retry rebuilds a byte-identical spec, and so a validator can be resumed
    later without consulting the source record again.
    """
    return {
        "run_id": validator_run_id,
        "revision": relation["source_revision"],
        "label": _validator_label(relation["validator_key"]),
        "task_id": _validator_task_id(relation["source_run_id"]),
        "queue_priority": "normal",
        "workload_class": VALIDATOR_WORKLOAD_CLASS,
        "submitted_command": command,
        "submitted_command_sha256": sha256_bytes(command.encode("utf-8")),
        "minimum_cores": 1,
        "requested_cores": requested_cores,
        "server_scope": "snapshot",
        "prepared_servers": prepared_servers,
        "output_relpath": _validator_output_relpath(validator_run_id),
        "output_path": None,
        "output_metadata": {},
        "output_sync": None,
        "lease_seconds": lease_seconds,
        "privacy": privacy,
        "derivation": relation,
    }


def _reader_source() -> str:
    path = Path(str(derived_result_remote.__file__))
    if not path.is_file():
        raise RuntimeError("installed derived result reader source is unavailable")
    return path.read_text(encoding="utf-8")


def _read_remote_result(
    *,
    ssh_target: str,
    remote_python: str,
    artifact_root: str,
    relpath: str,
    timeout: int,
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "artifact_root": artifact_root,
        "relpath": relpath,
        "max_bytes": derived_result_remote.MAX_RESULT_BYTES,
    }
    encoded = base64.urlsafe_b64encode(canonical_bytes(payload)).decode("ascii")
    remote_command = " ".join(
        (
            f"{derived_result_remote.PAYLOAD_ENV}={shlex.quote(encoded)}",
            remote_python_stdin_command(remote_python),
        )
    )
    argv = ["ssh", *ssh_connection_options(timeout), ssh_target, remote_command]
    try:
        completed = subprocess.run(
            argv,
            input=_reader_source(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout + _RESULT_READ_BUDGET_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ResultUnavailable(f"result read timed out after {exc.timeout}s") from exc
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or "reader returned no JSON"
        raise ResultUnavailable(f"result read failed: {detail}") from exc
    if not isinstance(response, dict):
        raise ResultUnavailable("result read returned invalid data")
    if response.get("ok") is not True:
        raise ResultUnavailable(f"result read refused: {response.get('error')}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ResultUnavailable("result read returned no result record")
    return result


def _retrieve_result(
    *,
    view: dict[str, Any],
    relation: dict[str, Any],
    candidate: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    sync = view.get("output_sync")
    if not isinstance(sync, dict) or sync.get("status") != "completed":
        raise ResultUnavailable("validator artifact is not synchronized")
    receipt = sync.get("receipt")
    if not isinstance(receipt, dict):
        raise ResultUnavailable("validator has no completed output-sync receipt")
    if receipt.get("verification") != CHECKSUM_VERIFICATION:
        raise ResultUnavailable("validator artifact is not checksum verified")
    artifact_root = normalize_artifact_path(
        receipt.get("target_path"), "validator receipt target_path"
    )
    runtime = candidate.get("runtime")
    if not isinstance(runtime, dict):
        raise ResultUnavailable("archive target has no configured project runtime")
    record = _read_remote_result(
        ssh_target=str(candidate["ssh"]),
        remote_python=str(runtime["python"]),
        artifact_root=artifact_root,
        relpath=relation["result_relpath"],
        timeout=timeout,
    )
    try:
        content = base64.b64decode(str(record["content_base64"]), validate=True)
    except (KeyError, ValueError) as exc:
        raise ResultUnavailable("result read returned undecodable content") from exc
    digest = sha256_bytes(content)
    if digest != record.get("sha256"):
        raise ResultUnavailable("result digest does not match the transported bytes")
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResultUnavailable("result file is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ResultUnavailable("result file must contain one JSON object")
    return {
        "relpath": relation["result_relpath"],
        "artifact_root": artifact_root,
        "path": record.get("path"),
        "size": record.get("size"),
        "sha256": digest,
        "payload": payload,
    }


def _wait_status(waited: dict[str, Any]) -> str:
    status = str(waited.get("wait_status"))
    if status == "timed_out":
        return "validation_pending"
    if status != "completed":
        return "attention_required"
    outcome = (waited.get("run_view") or {}).get("outcome")
    if outcome != "succeeded":
        return "validator_failed"
    return "validated"


def validate_run(args: argparse.Namespace) -> dict[str, Any]:
    """Derive, resume, observe, and retrieve one validation run."""
    try:
        return _validate_run(args)
    except ResultUnavailable as exc:
        return _result(status="result_unavailable", error=str(exc))
    except (OSError, RuntimeError, ValueError) as exc:
        return _result(status="invalid_request", error=str(exc))


def _validate_run(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    source_run_id = validate_current_run_id(args.source_run_id)
    validator_key = normalize_validator_key(args.validator_key)
    result_relpath = normalize_result_relpath(args.result_relpath)
    command = args.command
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ValueError("--command must be non-empty shell text without NUL bytes")
    requested_cores = normalize_requested_cores(
        getattr(args, "requested_cores", None) or 1
    )
    if requested_cores is None:  # pragma: no cover - normalization always returns int
        raise ValueError("--cores must be a positive integer")
    privacy = getattr(args, "privacy", None)

    lookup = _call(
        config,
        "validation-lookup",
        timeout=args.timeout,
        action_args=(
            "--source-run-id",
            source_run_id,
            "--validator-key",
            validator_key,
        ),
    )
    validator_run_id = validate_current_run_id(str(lookup["validator_run_id"]))
    existing = lookup.get("validator")

    if isinstance(existing, dict):
        relation = _existing_relation(existing)
        if relation["result_relpath"] != result_relpath:
            raise ValueError(
                f"validator run {validator_run_id} was frozen with result path "
                f"{relation['result_relpath']!r}; submit the changed validator "
                "under a new key"
            )
        disposition = "reused"
    else:
        facts = lookup.get("source")
        if not isinstance(facts, dict):
            raise ValueError(str(lookup.get("source_error") or "source run is unusable"))
        relation = build_relation(
            source_run_id=facts["source_run_id"],
            source_revision=facts["revision"],
            source_server=facts["server"],
            target_server=facts["artifact"]["target_server"],
            target_path=facts["artifact"]["target_path"],
            receipt_sha256=facts["artifact"]["receipt_sha256"],
            validator_key=validator_key,
            result_relpath=result_relpath,
        )
        disposition = None
    digest = validator_spec_sha256(
        relation,
        validator_run_id=validator_run_id,
        command=command,
        requested_cores=requested_cores,
        privacy=privacy,
    )
    if disposition == "reused":
        if digest != relation["spec_sha256"]:
            raise ValueError(
                f"validator run {validator_run_id} already exists with a different "
                "immutable spec; submit the changed validator under a new key"
            )
    else:
        relation = {**relation_identity(relation), "spec_sha256": digest}

    archive_target = relation["source_artifact"]["target_server"]
    candidate: dict[str, Any] | None = None
    if disposition is None:
        candidate, pool = _archive_candidate(
            config,
            args,
            archive_target=archive_target,
            requested_cores=requested_cores,
        )
        submitted = _submit(
            config,
            args,
            relation=relation,
            validator_run_id=validator_run_id,
            command=command,
            requested_cores=requested_cores,
            privacy=privacy,
            pool=pool,
        )
        disposition = str(submitted["outcome"]["submission_disposition"])

    source = _source_facts(relation)
    if not getattr(args, "wait", False):
        return _result(
            status="submitted",
            source=source,
            validator=_validator_facts(
                validator_run_id=validator_run_id,
                relation=relation,
                disposition=disposition,
                view=existing if isinstance(existing, dict) else None,
            ),
        )

    waited = waiting.wait_for_run(
        argparse.Namespace(
            project_config=args.project_config,
            run_id=validator_run_id,
            timeout=args.timeout,
            until="reportable",
            max_wait=getattr(args, "max_wait", None),
            connection_grace=getattr(args, "connection_grace", None),
        )
    )
    view = waited.get("run_view") or {}
    status = _wait_status(waited)
    validator = _validator_facts(
        validator_run_id=validator_run_id,
        relation=relation,
        disposition=disposition,
        view=view,
    )
    if status != "validated":
        return _result(
            status=status,
            source=source,
            validator=validator,
            wait=waited,
            error=view.get("attention_reason"),
        )
    if candidate is None:
        # A resumed validator reaches retrieval without a preparation pass, so the
        # archive target is probed here instead of on every detached call. The
        # lifecycle already completed, so an unreachable target is a retrieval
        # failure rather than an invalid request.
        try:
            candidate, _pool = _archive_candidate(
                config,
                args,
                archive_target=archive_target,
                requested_cores=requested_cores,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ResultUnavailable(
                f"archive target {archive_target!r} is unavailable for retrieval: {exc}"
            ) from exc
    retrieved = _retrieve_result(
        view=view,
        relation=relation,
        candidate=candidate,
        timeout=args.timeout,
    )
    return _result(
        status="validated",
        source=source,
        validator=validator,
        result=retrieved,
        wait=waited,
    )
