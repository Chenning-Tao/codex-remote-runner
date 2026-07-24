from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from remote_runner import cli
from remote_runner._internal import output_sync, registration, run_purge, task_purge
from remote_runner._internal.controller import run_purge as controller_run_purge
from remote_runner._internal.controller.registry import (
    controller_paths,
    create_run_tombstone,
    create_task_tombstone,
    list_jobs,
    load_run_tombstone,
    load_task_tombstone,
    run_purge_dir,
    submit_job,
    transition_queued_state,
)
from remote_runner._internal.execution_registry import (
    project_paths,
    sha256_bytes,
    update_current_state,
    write_yaml,
)


FAILED = "rr-0123456789abcdef"
REPLACEMENT = "rr-fedcba9876543210"
SIBLING = "rr-1111111111111111"
REVISION = "a" * 40
COMMAND = "python experiment.py --case formal"
TASK = "formal-bb-subthreshold-baseline-sweep"


def queued_job(
    run_id: str,
    *,
    command: str = COMMAND,
    task: str = TASK,
    output_path: str | None = None,
    output_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "revision": REVISION,
        "label": f"run {run_id}",
        "task_id": task,
        "result_intent": "candidate",
        "result_tags": {"campaign": "formal"},
        "workload_class": "standard",
        "submitted_command": command,
        "submitted_command_sha256": sha256_bytes(command.encode()),
        "worker_arg": "--num-workers",
        "prepared_servers": [
            {
                "name": "compute-a",
                "ssh": "compute-a",
                "ssh_profile": "test",
                "configured_cores": 8,
                "priority": 100,
                "bare_repo": "/srv/example/repo.git",
                "worktree_root": "/srv/example/worktrees",
                "python": sys.executable,
                "output_root": None,
            }
        ],
        "output_relpath": None,
        "output_path": output_path,
        "output_metadata": output_metadata or {"case": "formal"},
    }


def controller_args(
    root: Path,
    *,
    apply: bool,
    run_id: str = FAILED,
    replacement_run_id: str | None = None,
    no_replacement: bool = False,
    reason: str = "discard failed attempt",
) -> argparse.Namespace:
    return argparse.Namespace(
        controller_root=root,
        project_id="example",
        run_id=run_id,
        replacement_run_id=replacement_run_id,
        no_replacement=no_replacement,
        reason=reason,
        apply=apply,
        timeout=8,
    )


def fail_queue(paths: object, run_id: str) -> None:
    transition_queued_state(
        paths,  # type: ignore[arg-type]
        run_id,
        expected_revision=0,
        status="failed",
        error="launch failed",
    )


def dispatch_queue(paths: object, run_id: str) -> None:
    state = transition_queued_state(
        paths,  # type: ignore[arg-type]
        run_id,
        expected_revision=0,
        status="dispatching",
    )
    transition_queued_state(
        paths,  # type: ignore[arg-type]
        run_id,
        expected_revision=int(state["revision"]),
        status="dispatched",
    )


def register_execution(
    paths: object,
    run_id: str,
    *,
    status: str,
    command: str = COMMAND,
    task: str = TASK,
    output_path: str | None = None,
    output_metadata: dict[str, object] | None = None,
) -> object:
    registration.register(
        argparse.Namespace(
            project_config=paths.config_path,  # type: ignore[attr-defined]
            label=f"execution {run_id}",
            task_id=task,
            result_intent="candidate",
            result_tags={"campaign": "formal"},
            workload_class="standard",
            server="compute-a",
            ssh="compute-a",
            ssh_profile="test",
            configured_cores=8,
            minimum_cores=1,
            workers=8,
            command=command,
            remote_workdir=f"/srv/example/worktrees/{REVISION}",
            project_python=sys.executable,
            expected_revision=REVISION,
            source_revision=REVISION,
            prepared_servers=["compute-a"],
            submitted_command=command,
            worker_defaulted=False,
            require_clean_worktree=True,
            output_root=None,
            output_relpath=None,
            output_path=output_path,
            output_metadata=json.dumps(output_metadata or {"case": "formal"}),
            privacy=None,
            run_id=run_id,
        )
    )
    execution_paths = project_paths(paths.config_path)  # type: ignore[attr-defined]
    update_current_state(
        execution_paths,
        run_id,
        0,
        {
            "status": status,
            "finished_at": "2026-07-24T00:00:00Z",
            "exit_code": 0 if status == "succeeded" else 1,
        },
    )
    return execution_paths


def setup_failed_and_replacement(
    tmp_path: Path,
    *,
    replacement_command: str = COMMAND,
    failed_output: str = "/srv/example/output/failed",
    replacement_output: str = "/srv/example/output/replacement",
) -> tuple[object, object]:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(paths, queued_job(FAILED, output_path=failed_output))
    dispatch_queue(paths, FAILED)
    execution_paths = register_execution(
        paths,
        FAILED,
        status="failed",
        output_path=failed_output,
    )
    submit_job(
        paths,
        queued_job(
            REPLACEMENT,
            command=replacement_command,
            output_path=replacement_output,
        ),
    )
    dispatch_queue(paths, REPLACEMENT)
    register_execution(
        paths,
        REPLACEMENT,
        status="succeeded",
        command=replacement_command,
        output_path=replacement_output,
    )
    return paths, execution_paths


def test_cli_requires_explicit_replacement_policy() -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["purge-run", "--run-id", FAILED])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "purge-run",
                "--run-id",
                FAILED,
                "--replacement-run-id",
                REPLACEMENT,
                "--no-replacement",
            ]
        )

    args = parser.parse_args(["purge-run", "--run-id", FAILED, "--no-replacement"])
    assert args.apply is False
    assert args.no_replacement is True


def test_queue_only_failed_preview_is_read_only(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job(FAILED))
    fail_queue(paths, FAILED)

    result = controller_run_purge.purge_run(
        controller_args(paths.root, apply=False, no_replacement=True)
    )

    assert result["status"] == "ready"
    assert result["candidate"]["queue_status"] == "failed"
    assert result["candidate"]["runtime"] is False
    assert (paths.queue_dir / FAILED).is_dir()
    assert load_run_tombstone(paths, FAILED) is None
    assert not run_purge_dir(paths, FAILED).exists()
    assert not output_sync.output_sync_paths(paths.registry_root).root.exists()


def test_queue_only_failed_apply_removes_only_selected_record(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job(FAILED))
    fail_queue(paths, FAILED)
    submit_job(paths, queued_job(SIBLING))
    fail_queue(paths, SIBLING)

    result = controller_run_purge.purge_run(
        controller_args(paths.root, apply=True, no_replacement=True)
    )

    assert result["status"] == "complete"
    assert not (paths.queue_dir / FAILED).exists()
    assert (paths.queue_dir / SIBLING).is_dir()
    tombstone = load_run_tombstone(paths, FAILED)
    assert tombstone is not None
    assert tombstone["status"] == "purged"
    assert tombstone["replacement_policy"] == "explicit_none"
    assert load_task_tombstone(paths, TASK) is None
    with pytest.raises(ValueError, match="cannot be reused"):
        submit_job(paths, queued_job(FAILED))


def test_failed_execution_with_matching_replacement_purges_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = setup_failed_and_replacement(tmp_path)
    artifact_calls: list[str] = []
    monkeypatch.setattr(
        controller_run_purge,
        "purge_remote_run_artifacts",
        lambda **kwargs: (
            artifact_calls.append(str(kwargs["run_id"]))
            or {"ok": True, "action": "purged"}
        ),
    )
    monkeypatch.setattr(
        controller_run_purge,
        "purge_remote_worktree",
        lambda **_kwargs: pytest.fail("replacement keeps the shared worktree"),
    )

    preview = controller_run_purge.purge_run(
        controller_args(
            paths.root,
            apply=False,
            replacement_run_id=REPLACEMENT,
        )
    )
    assert preview["status"] == "ready"
    assert preview["replacement"]["matches"] is True

    result = controller_run_purge.purge_run(
        controller_args(
            paths.root,
            apply=True,
            replacement_run_id=REPLACEMENT,
        )
    )

    assert result["status"] == "complete"
    assert artifact_calls == [FAILED]
    assert not (paths.queue_dir / FAILED).exists()
    assert (paths.queue_dir / REPLACEMENT).is_dir()
    assert not (execution_paths.runs_dir / FAILED).exists()  # type: ignore[attr-defined]
    assert (execution_paths.runs_dir / REPLACEMENT).is_dir()  # type: ignore[attr-defined]
    events = execution_paths.events_path.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert FAILED not in events
    assert REPLACEMENT in events
    assert load_task_tombstone(paths, TASK) is None
    assert [job["run_id"] for job, _state in list_jobs(paths)] == [REPLACEMENT]


def test_replacement_provenance_mismatch_blocks_without_mutation(
    tmp_path: Path,
) -> None:
    paths, execution_paths = setup_failed_and_replacement(
        tmp_path,
        replacement_command="python experiment.py --case different",
    )

    result = controller_run_purge.purge_run(
        controller_args(
            paths.root,
            apply=True,
            replacement_run_id=REPLACEMENT,
        )
    )

    assert result["status"] == "blocked"
    assert "provenance does not match" in result["blockers"][0]["error"]
    assert (paths.queue_dir / FAILED).is_dir()
    assert (execution_paths.runs_dir / FAILED).is_dir()  # type: ignore[attr-defined]
    assert load_run_tombstone(paths, FAILED) is None


def test_output_overlap_with_replacement_blocks_apply(tmp_path: Path) -> None:
    paths, _execution_paths = setup_failed_and_replacement(
        tmp_path,
        failed_output="/srv/example/output",
        replacement_output="/srv/example/output/replacement",
    )

    result = controller_run_purge.purge_run(
        controller_args(
            paths.root,
            apply=True,
            replacement_run_id=REPLACEMENT,
        )
    )

    assert result["status"] == "blocked"
    overlap = next(
        blocker
        for blocker in result["blockers"]
        if blocker.get("retained_run_id") == REPLACEMENT
    )
    assert overlap["output_path"] == "/srv/example/output"
    assert load_run_tombstone(paths, FAILED) is None


def test_succeeded_target_is_not_eligible(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(paths, queued_job(FAILED))
    dispatch_queue(paths, FAILED)
    register_execution(paths, FAILED, status="succeeded")

    result = controller_run_purge.purge_run(
        controller_args(paths.root, apply=True, no_replacement=True)
    )

    assert result["status"] == "blocked"
    assert "not failed" in result["blockers"][0]["error"]
    assert load_run_tombstone(paths, FAILED) is None


def test_succeeded_output_sync_evidence_blocks_failed_run_purge(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job(FAILED))
    fail_queue(paths, FAILED)
    sync_paths = output_sync.output_sync_paths(paths.registry_root)
    sync_paths.completed_dir.mkdir(parents=True)
    (sync_paths.completed_dir / f"{FAILED}.json").write_text(
        json.dumps(
            {
                "receipt": {
                    "run_id": FAILED,
                    "source_deletion_performed": False,
                }
            }
        ),
        encoding="utf-8",
    )

    result = controller_run_purge.purge_run(
        controller_args(paths.root, apply=True, no_replacement=True)
    )

    assert result["status"] == "blocked"
    assert "not a failed-state cancellation" in result["blockers"][0]["error"]
    assert (paths.queue_dir / FAILED).is_dir()


def test_unknown_remote_outcome_resumes_same_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = setup_failed_and_replacement(tmp_path)
    attempts = 0

    def purge_artifacts(**_kwargs: object) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise task_purge.PurgeOutcomeUnknown("transport outcome unknown")
        return {"ok": True, "action": "already_absent"}

    monkeypatch.setattr(
        controller_run_purge,
        "purge_remote_run_artifacts",
        purge_artifacts,
    )
    monkeypatch.setattr(
        controller_run_purge,
        "purge_remote_worktree",
        lambda **_kwargs: pytest.fail("replacement keeps the shared worktree"),
    )
    args = controller_args(
        paths.root,
        apply=True,
        replacement_run_id=REPLACEMENT,
    )

    first = controller_run_purge.purge_run(args)
    assert first["status"] == "attention_required"
    assert not (execution_paths.runs_dir / FAILED).exists()  # type: ignore[attr-defined]
    assert run_purge_dir(paths, FAILED).is_dir()
    assert load_run_tombstone(paths, FAILED)["status"] == "purging"  # type: ignore[index]

    second = controller_run_purge.purge_run(args)
    assert second["status"] == "complete"
    assert attempts == 2


def test_event_compaction_failure_resumes_without_redeleting_remote_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _execution_paths = setup_failed_and_replacement(tmp_path)
    remote_calls = 0

    def purge_artifacts(**_kwargs: object) -> dict[str, object]:
        nonlocal remote_calls
        remote_calls += 1
        return {"ok": True, "action": "purged"}

    monkeypatch.setattr(
        controller_run_purge,
        "purge_remote_run_artifacts",
        purge_artifacts,
    )
    monkeypatch.setattr(
        controller_run_purge,
        "compact_run_events",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("malformed matching event")
        ),
    )
    args = controller_args(
        paths.root,
        apply=True,
        replacement_run_id=REPLACEMENT,
    )

    first = controller_run_purge.purge_run(args)
    assert first["status"] == "attention_required"
    assert "malformed matching event" in first["failures"][0]["error"]
    assert remote_calls == 1

    monkeypatch.setattr(
        controller_run_purge,
        "compact_run_events",
        lambda *_args, **_kwargs: {"removed": 2, "preserved": 3},
    )
    second = controller_run_purge.purge_run(args)
    assert second["status"] == "complete"
    assert remote_calls == 1


def test_replacement_run_cannot_be_purged_after_it_is_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _execution_paths = setup_failed_and_replacement(tmp_path)
    monkeypatch.setattr(
        controller_run_purge,
        "purge_remote_run_artifacts",
        lambda **_kwargs: {"ok": True, "action": "purged"},
    )

    completed = controller_run_purge.purge_run(
        controller_args(
            paths.root,
            apply=True,
            replacement_run_id=REPLACEMENT,
        )
    )
    assert completed["status"] == "complete"

    preview = controller_run_purge.purge_run(
        controller_args(
            paths.root,
            apply=False,
            run_id=REPLACEMENT,
            no_replacement=True,
        )
    )
    assert preview["status"] == "blocked"
    assert any(
        blocker.get("dependent_run_id") == FAILED for blocker in preview["blockers"]
    )


def test_no_replacement_removes_exclusively_owned_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(paths, queued_job(FAILED))
    dispatch_queue(paths, FAILED)
    register_execution(paths, FAILED, status="failed")
    worktrees: list[str] = []
    monkeypatch.setattr(
        controller_run_purge,
        "purge_remote_run_artifacts",
        lambda **_kwargs: {"ok": True, "action": "purged"},
    )
    monkeypatch.setattr(
        controller_run_purge,
        "purge_remote_worktree",
        lambda **kwargs: (
            worktrees.append(str(kwargs["remote_workdir"]))
            or {"ok": True, "action": "removed"}
        ),
    )

    result = controller_run_purge.purge_run(
        controller_args(paths.root, apply=True, no_replacement=True)
    )

    assert result["status"] == "complete"
    assert worktrees == [f"/srv/example/worktrees/{REVISION}"]
    with pytest.raises(FileExistsError, match="cannot be reused"):
        register_execution(paths, FAILED, status="failed")


def test_completed_retry_requires_the_frozen_policy(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job(FAILED))
    fail_queue(paths, FAILED)
    completed = controller_run_purge.purge_run(
        controller_args(paths.root, apply=True, no_replacement=True)
    )
    assert completed["status"] == "complete"

    with pytest.raises(ValueError, match="stored tombstone"):
        controller_run_purge.purge_run(
            controller_args(
                paths.root,
                apply=False,
                replacement_run_id=REPLACEMENT,
            )
        )


def test_failed_sync_cancellation_state_is_removed(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job(FAILED))
    fail_queue(paths, FAILED)
    sync_paths = output_sync.output_sync_paths(paths.registry_root)
    for directory in (
        sync_paths.pending_dir,
        sync_paths.completed_dir,
        sync_paths.state_dir,
    ):
        directory.mkdir(parents=True)
    (sync_paths.pending_dir / f"{FAILED}.json").write_text("{}\n", encoding="utf-8")
    (sync_paths.state_dir / f"{FAILED}.json").write_text("{}\n", encoding="utf-8")
    (sync_paths.completed_dir / f"{FAILED}.json").write_text(
        json.dumps(
            {
                "receipt": {
                    "run_id": FAILED,
                    "disposition": "cancelled_before_sync",
                    "authoritative_status": "failed",
                }
            }
        ),
        encoding="utf-8",
    )

    result = controller_run_purge.purge_run(
        controller_args(paths.root, apply=True, no_replacement=True)
    )

    assert result["status"] == "complete"
    assert not any(
        (directory / f"{FAILED}.json").exists()
        for directory in (
            sync_paths.pending_dir,
            sync_paths.completed_dir,
            sync_paths.state_dir,
        )
    )


def test_run_and_task_purge_markers_are_mutually_exclusive(tmp_path: Path) -> None:
    digest = "sha256:" + "a" * 64
    run_first = controller_paths(tmp_path / "run-first", "example")
    create_run_tombstone(
        run_first,
        FAILED,
        task_id=TASK,
        reason="discard",
        replacement_policy="explicit_none",
        replacement_run_id=None,
        target_provenance_sha256=digest,
        replacement_provenance_sha256=None,
    )
    with pytest.raises(RuntimeError, match="one of its runs is purging"):
        create_task_tombstone(run_first, TASK, reason="discard task")

    task_first = controller_paths(tmp_path / "task-first", "example")
    create_task_tombstone(task_first, TASK, reason="discard task")
    with pytest.raises(RuntimeError, match="task is purging"):
        create_run_tombstone(
            task_first,
            FAILED,
            task_id=TASK,
            reason="discard",
            replacement_policy="explicit_none",
            replacement_run_id=None,
            target_provenance_sha256=digest,
            replacement_provenance_sha256=None,
        )


def test_public_client_forwards_explicit_replacement_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / ".remote-runner.yaml"
    write_yaml(
        config,
        {
            "project_id": "example",
            "controller": {"ssh": "controller_host", "root": "/controller"},
            "source": {"local_repo": "code"},
            "remote": {
                "compute-a": {
                    "bare_repo": "/srv/repo.git",
                    "worktree_root": "/srv/worktrees",
                    "python": "/opt/python3",
                }
            },
        },
    )
    observed: dict[str, object] = {}

    def controller(
        _config,
        action: str,
        *,
        timeout: int,
        action_args: tuple[str, ...],
        overall_timeout: int,
    ) -> dict[str, object]:
        observed.update(
            action=action,
            timeout=timeout,
            action_args=action_args,
            overall_timeout=overall_timeout,
        )
        return {"status": "ready"}

    monkeypatch.setattr(run_purge, "call_controller", controller)

    result = run_purge.request_run_purge(
        argparse.Namespace(
            project_config=config,
            run_id=FAILED,
            replacement_run_id=REPLACEMENT,
            no_replacement=False,
            reason="discard",
            apply=True,
            timeout=7,
        )
    )

    assert result == {"status": "ready"}
    assert observed == {
        "action": "purge-run",
        "timeout": 7,
        "action_args": (
            "--run-id",
            FAILED,
            "--reason",
            "discard",
            "--replacement-run-id",
            REPLACEMENT,
            "--apply",
        ),
        "overall_timeout": 3600,
    }
