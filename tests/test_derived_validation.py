from __future__ import annotations

import argparse
import io
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from remote_runner._internal import registration
from remote_runner._internal.controller import service as controller_service
from remote_runner._internal.controller.registry import (
    controller_paths,
    create_run_tombstone,
    ensure_derived_job,
    load_job,
    submit_job,
    transition_queued_state,
)
from remote_runner._internal.controller.validation_gate import source_facts
from remote_runner._internal.derivation import build_relation, derived_run_id
from remote_runner._internal.derived_validation import (
    build_validator_job,
    validator_spec_sha256,
)
from remote_runner._internal.launch_plan import (
    SOURCE_CONTEXT_ENVIRONMENT,
    build_launch_plan,
)
from remote_runner._internal.execution_registry import (
    load_yaml,
    project_paths,
    sha256_bytes,
    update_current_state,
    write_yaml,
)
from remote_runner._internal.output_sync import store_config


PROJECT_ID = "example"
SOURCE_RUN_ID = "rr-0123456789abcdef"
REVISION = "a" * 40
SOURCE_SERVER = "compute-a"
ARCHIVE_TARGET = "archive"
ARCHIVE_ROOT = "/srv/archive/scientific-v1"
SOURCE_OUTPUT_ROOT = "/srv/outputs"
SOURCE_OUTPUT_RELPATH = "runs/source"
VALIDATOR_KEY = "portable-smoke/v1"
VALIDATOR_COMMAND = "experiments/remote_runner/run_synced_validator.sh"


def prepared_server(name: str, *, output_root: str, test_slots: int = 0) -> dict[str, Any]:
    return {
        "name": name,
        "ssh": name,
        "ssh_profile": "intranet",
        "configured_cores": 32,
        "priority": 100,
        "bare_repo": "/srv/example/repo.git",
        "worktree_root": "/srv/example/worktrees",
        "python": "/opt/example/bin/python3",
        "output_root": output_root,
        "test_slots": test_slots,
    }


def source_job() -> dict[str, Any]:
    command = "python producer.py"
    return {
        "run_id": SOURCE_RUN_ID,
        "revision": REVISION,
        "label": "producer",
        "task_id": "cohort-1",
        "submitted_command": command,
        "submitted_command_sha256": sha256_bytes(command.encode()),
        "prepared_servers": [
            prepared_server(SOURCE_SERVER, output_root=SOURCE_OUTPUT_ROOT)
        ],
        "output_relpath": SOURCE_OUTPUT_RELPATH,
        "output_path": None,
        "output_metadata": {},
    }


def sync_config_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "target_server": ARCHIVE_TARGET,
        "target_ssh": ARCHIVE_TARGET,
        "target_root": ARCHIVE_ROOT,
        "target_python": "/opt/python3",
        "source_ssh_config": "/home/user/.ssh/output-sync.conf",
        "source_hosts": {SOURCE_SERVER: "compute-a-int"},
        "retry_seconds": 60,
    }


def receipt(**overrides: Any) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "run_id": SOURCE_RUN_ID,
        "source_server": SOURCE_SERVER,
        "source_path": f"{SOURCE_OUTPUT_ROOT}/{SOURCE_OUTPUT_RELPATH}",
        "source_kind": "directory",
        "target_path": f"{ARCHIVE_ROOT}/artifacts/{SOURCE_RUN_ID}",
        "revision": REVISION,
        "task_id": "cohort-1",
        "authoritative_status": "succeeded",
        "terminal_at": "2026-08-20T00:00:00Z",
        "archived_at": "2026-08-20T00:05:00Z",
        "verification": "rsync_checksum_dry_run",
        "disposition": "copied_and_verified",
        "source_deletion_performed": False,
    }
    payload.update(overrides)
    return payload


def register_execution(
    paths: Any,
    *,
    run_id: str = SOURCE_RUN_ID,
    server: str = SOURCE_SERVER,
    revision: str = REVISION,
    label: str = "producer",
    task_id: str = "cohort-1",
    workload_class: str = "standard",
    output_root: str = SOURCE_OUTPUT_ROOT,
    output_relpath: str = SOURCE_OUTPUT_RELPATH,
    command: str = "python producer.py",
    derivation: dict[str, Any] | None = None,
) -> None:
    args = argparse.Namespace(
        project_config=paths.config_path,
        label=label,
        task_id=task_id,
        workload_class=workload_class,
        server=server,
        machine_id=None,
        machine_fingerprint=None,
        ssh=server,
        ssh_profile="intranet",
        configured_cores=32,
        minimum_cores=1,
        requested_cores=None,
        assigned_cores=32,
        command=command,
        remote_workdir="/srv/example/worktrees/current",
        project_python="/opt/example/bin/python3",
        source_revision=revision,
        prepared_servers=[server],
        submitted_command=command,
        expected_revision=revision,
        require_clean_worktree=True,
        output_root=output_root,
        output_relpath=output_relpath,
        output_path=f"{output_root}/{output_relpath}",
        output_metadata="{}",
        run_id=run_id,
        privacy=None,
        derivation=derivation,
    )
    registration.register(args)


def complete_sync(paths: Any, *, run_id: str = SOURCE_RUN_ID, **overrides: Any) -> None:
    completed_dir = paths.registry_root / "output-sync" / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "intent": {"run_id": run_id, "source_server": SOURCE_SERVER},
        "receipt": receipt(run_id=run_id, **overrides),
        "confirmed_at": "2026-08-20T00:06:00Z",
    }
    (completed_dir / f"{run_id}.json").write_text(
        json.dumps(record, sort_keys=True), encoding="utf-8"
    )


def controller_with_source(
    tmp_path: Path,
    *,
    execution_status: str = "succeeded",
    synchronized: bool = True,
    **receipt_overrides: Any,
) -> Any:
    paths = controller_paths(tmp_path / "controller", PROJECT_ID)
    write_yaml(paths.config_path, {"controller_registry": True})
    store_config(paths.registry_root, sync_config_payload())
    submit_job(paths, source_job())
    transition_queued_state(paths, SOURCE_RUN_ID, expected_revision=0, status="dispatching")
    transition_queued_state(paths, SOURCE_RUN_ID, expected_revision=1, status="dispatched")
    register_execution(paths)
    update_current_state(
        project_paths(paths.config_path),
        SOURCE_RUN_ID,
        0,
        {
            "status": execution_status,
            "started_at": "2026-08-19T23:00:00+00:00",
            "finished_at": "2026-08-20T00:00:00+00:00",
            "exit_code": 0 if execution_status == "succeeded" else 1,
        },
    )
    if synchronized:
        complete_sync(paths, **receipt_overrides)
    return paths


def relation_for(facts: dict[str, Any], *, validator_key: str = VALIDATOR_KEY) -> dict[str, Any]:
    return build_relation(
        source_run_id=facts["source_run_id"],
        source_revision=facts["revision"],
        source_server=facts["server"],
        target_server=facts["artifact"]["target_server"],
        target_path=facts["artifact"]["target_path"],
        receipt_sha256=facts["artifact"]["receipt_sha256"],
        validator_key=validator_key,
        result_relpath="acceptance.json",
    )


def validator_job(
    facts: dict[str, Any],
    *,
    validator_key: str = VALIDATOR_KEY,
    command: str = VALIDATOR_COMMAND,
    requested_cores: int = 1,
) -> dict[str, Any]:
    relation = relation_for(facts, validator_key=validator_key)
    run_id = derived_run_id(
        project_id=PROJECT_ID,
        source_run_id=relation["source_run_id"],
        validator_key=relation["validator_key"],
    )
    relation["spec_sha256"] = validator_spec_sha256(
        relation,
        validator_run_id=run_id,
        command=command,
        requested_cores=requested_cores,
        privacy=None,
    )
    return build_validator_job(
        relation,
        validator_run_id=run_id,
        command=command,
        requested_cores=requested_cores,
        privacy=None,
        prepared_servers=[
            prepared_server(ARCHIVE_TARGET, output_root="/srv/outputs", test_slots=2)
        ],
        lease_seconds=900,
    )


def test_source_gate_returns_the_frozen_identity(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)

    facts = source_facts(paths, SOURCE_RUN_ID)

    assert facts["source_run_id"] == SOURCE_RUN_ID
    assert facts["revision"] == REVISION
    assert facts["server"] == SOURCE_SERVER
    assert facts["artifact"]["target_server"] == ARCHIVE_TARGET
    assert facts["artifact"]["target_path"] == f"{ARCHIVE_ROOT}/artifacts/{SOURCE_RUN_ID}"
    assert facts["artifact"]["receipt_sha256"].startswith("sha256:")


def test_source_gate_refuses_a_missing_run(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", PROJECT_ID)

    with pytest.raises(ValueError, match="no controller queue record"):
        source_facts(paths, SOURCE_RUN_ID)


def test_source_gate_refuses_an_unsynchronized_run(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path, synchronized=False)

    with pytest.raises(ValueError, match="not reportable yet"):
        source_facts(paths, SOURCE_RUN_ID)


def test_source_gate_refuses_a_failed_run(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path, execution_status="failed", synchronized=False)

    with pytest.raises(ValueError, match="did not succeed"):
        source_facts(paths, SOURCE_RUN_ID)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"verification": "rsync_copy"}, "not checksum verified"),
        ({"revision": "b" * 40}, "receipt revision disagrees"),
        ({"source_server": "compute-b"}, "receipt server disagrees"),
        ({"authoritative_status": "failed"}, "non-succeeded execution"),
        ({"target_path": "/srv/elsewhere/artifacts/x"}, "outside the configured archive root"),
    ],
)
def test_source_gate_refuses_receipt_identity_mismatch(
    tmp_path: Path,
    overrides: dict[str, Any],
    message: str,
) -> None:
    paths = controller_with_source(tmp_path, **overrides)

    with pytest.raises(ValueError, match=message):
        source_facts(paths, SOURCE_RUN_ID)


def test_source_gate_refuses_recursive_derivation(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    created = ensure_derived_job(paths, validator_job(facts))

    with pytest.raises(ValueError, match="derived validation run"):
        source_facts(paths, created["run_id"])


def test_ensure_derived_job_creates_then_reuses_one_run(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)

    created = ensure_derived_job(paths, validator_job(facts))
    reused = ensure_derived_job(paths, validator_job(facts))

    assert created["disposition"] == "created"
    assert reused["disposition"] == "reused"
    assert reused["run_id"] == created["run_id"]
    assert {path.name for path in paths.queue_dir.iterdir()} == {
        SOURCE_RUN_ID,
        created["run_id"],
    }
    job, state = load_job(paths, created["run_id"])
    assert job["workload_class"] == "test"
    assert job["requested_cores"] == 1
    assert job["derivation"]["source_run_id"] == SOURCE_RUN_ID
    assert state["status"] == "queued"


def test_ensure_derived_job_is_deterministic_across_projects(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)

    created = ensure_derived_job(paths, validator_job(facts))

    assert created["run_id"] == derived_run_id(
        project_id=PROJECT_ID,
        source_run_id=SOURCE_RUN_ID,
        validator_key=VALIDATOR_KEY,
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"command": "python other_validator.py"},
        {"requested_cores": 4},
    ],
)
def test_ensure_derived_job_conflicts_on_a_changed_spec(
    tmp_path: Path,
    changed: dict[str, Any],
) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    ensure_derived_job(paths, validator_job(facts))

    with pytest.raises(ValueError, match="different"):
        ensure_derived_job(paths, validator_job(facts, **changed))

    assert len(list(paths.queue_dir.iterdir())) == 2


def test_a_changed_validator_key_creates_a_separate_run(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)

    first = ensure_derived_job(paths, validator_job(facts))
    second = ensure_derived_job(paths, validator_job(facts, validator_key="portable-smoke/v2"))

    assert first["run_id"] != second["run_id"]


def test_failed_validator_exact_retry_reuses_the_same_run(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    created = ensure_derived_job(paths, validator_job(facts))
    transition_queued_state(
        paths,
        created["run_id"],
        expected_revision=0,
        status="failed",
        error="validator failed",
    )

    reused = ensure_derived_job(paths, validator_job(facts))

    assert reused["disposition"] == "reused"
    assert reused["run_id"] == created["run_id"]
    assert reused["queue_status"] == "failed"
    assert len(list(paths.queue_dir.iterdir())) == 2


def test_ensure_derived_job_rejects_a_foreign_run_id(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    job = validator_job(facts)
    job["run_id"] = "rr-fedcba9876543210"

    with pytest.raises(ValueError, match="does not match the derived identity"):
        ensure_derived_job(paths, job)


def test_ensure_derived_job_rejects_a_forged_spec_digest(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    job = validator_job(facts)
    job["submitted_command"] = "python elsewhere.py"
    job["submitted_command_sha256"] = sha256_bytes(b"python elsewhere.py")

    with pytest.raises(ValueError, match="spec digest does not match"):
        ensure_derived_job(paths, job)


def test_ensure_derived_job_refuses_a_purged_identity(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    job = validator_job(facts)
    create_run_tombstone(paths, job["run_id"])

    with pytest.raises(ValueError, match="has been purged"):
        ensure_derived_job(paths, job)


def test_derived_job_must_use_the_test_lane(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    job = validator_job(facts)
    job["workload_class"] = "standard"

    with pytest.raises(ValueError, match="test workload class"):
        ensure_derived_job(paths, job)


def test_ordinary_job_cannot_carry_a_derivation(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    job = source_job()
    job["run_id"] = "rr-fedcba9876543210"
    job["derivation"] = validator_job(facts)["derivation"]

    with pytest.raises(ValueError, match="only derived queued jobs"):
        submit_job(paths, job)


def test_derived_job_survives_an_ordinary_queue_read(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    created = ensure_derived_job(paths, validator_job(facts))

    job, _state = load_job(paths, created["run_id"])

    assert job["schema_version"] == 6
    assert job["derivation"]["spec_sha256"].startswith("sha256:")


def test_concurrent_identical_submissions_converge_on_one_run(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    barrier = threading.Barrier(4)
    outcomes: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def submit() -> None:
        job = validator_job(facts)
        barrier.wait(timeout=10)
        try:
            outcome = ensure_derived_job(paths, job)
        except BaseException as exc:  # pragma: no cover - reported below
            with lock:
                errors.append(exc)
            return
        with lock:
            outcomes.append(outcome)

    workers = [threading.Thread(target=submit) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert not errors
    assert len(outcomes) == 4
    assert len({outcome["run_id"] for outcome in outcomes}) == 1
    assert [outcome["disposition"] for outcome in outcomes].count("created") == 1
    assert len(list(paths.queue_dir.iterdir())) == 2


def controller_args(paths: Any) -> argparse.Namespace:
    return argparse.Namespace(
        controller_root=paths.root,
        project_id=PROJECT_ID,
        timeout=8,
        interval=60,
    )


def test_submit_validation_action_freezes_the_current_source_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    monkeypatch.setattr(controller_service, "ensure_dispatcher", lambda **_kwargs: False)
    monkeypatch.setattr(
        controller_service, "ensure_server_capacities", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(validator_job(facts)))
    )

    result = controller_service.submit_validation(controller_args(paths))

    assert result["outcome"]["submission_disposition"] == "created"
    assert result["source"]["artifact"]["target_server"] == ARCHIVE_TARGET
    assert result["outcome"]["run_id"] == derived_run_id(
        project_id=PROJECT_ID,
        source_run_id=SOURCE_RUN_ID,
        validator_key=VALIDATOR_KEY,
    )


def test_submit_validation_action_rejects_a_stale_relation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    stale = validator_job(facts)
    stale["derivation"]["source_artifact"]["receipt_sha256"] = "sha256:" + "f" * 64
    monkeypatch.setattr(controller_service, "ensure_dispatcher", lambda **_kwargs: False)
    monkeypatch.setattr(
        controller_service, "ensure_server_capacities", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stale)))

    with pytest.raises(ValueError, match="does not match the current source authority"):
        controller_service.submit_validation(controller_args(paths))

    assert len(list(paths.queue_dir.iterdir())) == 1


def test_submit_validation_action_requires_archive_target_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    misplaced = validator_job(facts)
    misplaced["prepared_servers"] = [
        prepared_server(SOURCE_SERVER, output_root="/srv/outputs", test_slots=2)
    ]
    misplaced["eligible_servers"] = [SOURCE_SERVER]
    monkeypatch.setattr(controller_service, "ensure_dispatcher", lambda **_kwargs: False)
    monkeypatch.setattr(
        controller_service, "ensure_server_capacities", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(misplaced)))

    with pytest.raises(ValueError, match="must be exactly the archive target"):
        controller_service.submit_validation(controller_args(paths))


def test_validation_lookup_reports_identity_before_any_validator_exists(
    tmp_path: Path,
) -> None:
    paths = controller_with_source(tmp_path)
    args = controller_args(paths)
    args.source_run_id = SOURCE_RUN_ID
    args.validator_key = VALIDATOR_KEY

    lookup = controller_service.validation_lookup(args)

    assert lookup["validator"] is None
    assert lookup["source_error"] is None
    assert lookup["source"]["revision"] == REVISION
    assert lookup["validator_run_id"] == derived_run_id(
        project_id=PROJECT_ID,
        source_run_id=SOURCE_RUN_ID,
        validator_key=VALIDATOR_KEY,
    )


def test_validation_lookup_keeps_an_existing_validator_observable(
    tmp_path: Path,
) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    created = ensure_derived_job(paths, validator_job(facts))
    (paths.registry_root / "output-sync" / "completed" / f"{SOURCE_RUN_ID}.json").unlink()
    args = controller_args(paths)
    args.source_run_id = SOURCE_RUN_ID
    args.validator_key = VALIDATOR_KEY

    lookup = controller_service.validation_lookup(args)

    assert lookup["source"] is None
    assert "not reportable" in lookup["source_error"]
    assert lookup["validator"]["run_id"] == created["run_id"]
    assert lookup["validator"]["queue"]["derivation"]["validator_key"] == VALIDATOR_KEY


def wrapper_source(paths: Any, run_id: str) -> str:
    plan = build_launch_plan(project_paths(paths.config_path), run_id)
    return next(
        asset.content.decode() for asset in plan.assets if asset.name == "run.sh"
    )


def register_validator_execution(paths: Any, run_id: str) -> dict[str, Any]:
    job, _state = load_job(paths, run_id)
    register_execution(
        paths,
        run_id=run_id,
        server=ARCHIVE_TARGET,
        label=str(job["label"]),
        task_id=str(job["task_id"]),
        workload_class="test",
        output_root="/srv/outputs",
        output_relpath=str(job["output_relpath"]),
        command=str(job["submitted_command"]),
        derivation=job["derivation"],
    )
    return job


def test_launch_environment_carries_the_frozen_source_identity(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    created = ensure_derived_job(paths, validator_job(facts))
    register_validator_execution(paths, created["run_id"])

    wrapper = wrapper_source(paths, created["run_id"])

    assert f"RR_SOURCE_RUN_ID={SOURCE_RUN_ID}" in wrapper
    assert f"RR_SOURCE_REVISION={REVISION}" in wrapper
    assert f"RR_SOURCE_SERVER={SOURCE_SERVER}" in wrapper
    assert (
        f"RR_SOURCE_ARTIFACT_PATH={ARCHIVE_ROOT}/artifacts/{SOURCE_RUN_ID}" in wrapper
    )
    assert "RR_VALIDATOR_KEY=portable-smoke/v1" in wrapper


def test_launch_environment_of_an_ordinary_run_gains_no_source_identity(
    tmp_path: Path,
) -> None:
    paths = controller_with_source(tmp_path)

    wrapper = wrapper_source(paths, SOURCE_RUN_ID)

    assert "RR_SOURCE_RUN_ID=" not in wrapper
    assert "RR_VALIDATOR_KEY=" not in wrapper


def test_workload_wrapper_clears_inherited_source_identity(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)

    wrapper = wrapper_source(paths, SOURCE_RUN_ID)

    unset = next(line for line in wrapper.splitlines() if line.startswith("unset "))
    assert set(SOURCE_CONTEXT_ENVIRONMENT).issubset(set(unset.split()))


def test_launch_refuses_a_manifest_with_an_unusable_relation(tmp_path: Path) -> None:
    paths = controller_with_source(tmp_path)
    facts = source_facts(paths, SOURCE_RUN_ID)
    created = ensure_derived_job(paths, validator_job(facts))
    job = register_validator_execution(paths, created["run_id"])
    execution_paths = project_paths(paths.config_path)
    manifest_path = execution_paths.runs_dir / created["run_id"] / "manifest.yaml"
    manifest = load_yaml(manifest_path)
    manifest["derivation"] = {**job["derivation"], "source_revision": "b" * 39}
    write_yaml(manifest_path, manifest)

    with pytest.raises(ValueError, match="source_revision"):
        wrapper_source(paths, created["run_id"])
