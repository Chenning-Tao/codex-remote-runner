from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from remote_runner._internal import output_sync, registration
from remote_runner._internal.controller import run_purge
from remote_runner._internal.controller.registry import (
    controller_paths,
    create_run_tombstone,
    load_job,
    load_run_tombstone,
    submit_job,
    transition_queued_state,
)
from remote_runner._internal.controller.run_view import load_run_view
from remote_runner._internal.execution_registry import (
    project_paths,
    sha256_bytes,
    stage_failed_current_run,
    update_current_state,
    write_yaml,
)


RUN_ID = "rr-0123456789abcdef"


def job(
    run_id: str = RUN_ID,
    *,
    output_path: str | None = None,
    output_sync_config: dict[str, object] | None = None,
) -> dict[str, object]:
    command = "python workload.py"
    return {
        "run_id": run_id,
        "revision": "a" * 40,
        "label": "failed attempt",
        "task_id": "task-1",
        "submitted_command": command,
        "submitted_command_sha256": sha256_bytes(command.encode()),
        "prepared_servers": [
            {
                "name": "compute-a",
                "ssh": "compute-a",
                "ssh_profile": "auto",
                "configured_cores": 8,
                "priority": 0,
                "bare_repo": "/srv/repo.git",
                "worktree_root": "/srv/worktrees",
                "python": "/opt/python3",
                "output_root": "/srv/output",
            }
        ],
        "output_relpath": None,
        "output_path": output_path,
        "output_metadata": {},
        "output_sync": output_sync_config,
        "lease_seconds": 120,
    }


def args(root: Path, *, apply: bool, delete_artifacts: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        controller_root=root,
        project_id="example",
        run_id=RUN_ID,
        reason="discard misleading failed record",
        apply=apply,
        delete_artifacts=delete_artifacts,
        timeout=8,
    )


def failed_queue(root: Path):
    paths = controller_paths(root, "example")
    submit_job(paths, job())
    transition_queued_state(
        paths, RUN_ID, expected_revision=0, status="failed", error="bad input"
    )
    return paths


def sync_config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "target_server": "archive",
        "target_ssh": "archive",
        "target_root": "/srv/archive/scientific-v1",
        "target_python": sys.executable,
        "source_ssh_config": "/home/user/.ssh/output-sync.conf",
        "source_hosts": {"compute-a": "compute-a-int"},
        "retry_seconds": 60,
        "paused": False,
    }


def failed_execution(root: Path):
    paths = controller_paths(root, "example")
    config = sync_config()
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(
        paths,
        job(
            output_path="/srv/output/missing",
            output_sync_config=config,
        ),
    )
    state = transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=0,
        status="dispatching",
    )
    transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=int(state["revision"]),
        status="dispatched",
    )
    registration.register(
        argparse.Namespace(
            project_config=paths.config_path,
            label="failed attempt",
            task_id="task-1",
            workload_class="standard",
            server="compute-a",
            ssh="compute-a",
            ssh_profile="auto",
            configured_cores=8,
            minimum_cores=1,
            assigned_cores=8,
            command="python workload.py",
            remote_workdir="/srv/worktrees/" + "a" * 40,
            project_python=sys.executable,
            expected_revision="a" * 40,
            source_revision="a" * 40,
            prepared_servers=["compute-a"],
            submitted_command="python workload.py",
            require_clean_worktree=True,
            output_root=None,
            output_relpath=None,
            output_path="/srv/output/missing",
            output_metadata=None,
            privacy=None,
            run_id=RUN_ID,
        )
    )
    execution_paths = project_paths(paths.config_path)
    output_sync.store_config(execution_paths.registry_root, config)
    update_current_state(
        execution_paths,
        RUN_ID,
        0,
        {
            "status": "failed",
            "finished_at": "2026-01-01T00:00:00Z",
            "exit_code": 1,
            "error": "bad input",
        },
    )
    return paths, execution_paths


def test_purge_run_needs_no_replacement_and_hides_minimal_tombstone(tmp_path: Path) -> None:
    paths = failed_queue(tmp_path / "controller")

    preview = run_purge.purge_run(args(paths.root, apply=False))
    assert preview["status"] == "ready"
    assert preview["candidate"]["run_id"] == RUN_ID
    assert "replacement" not in preview

    applied = run_purge.purge_run(args(paths.root, apply=True))
    assert applied["status"] == "complete"
    assert applied["artifacts_deleted"] is False
    with pytest.raises(FileNotFoundError):
        load_job(paths, RUN_ID)

    tombstone = load_run_tombstone(paths, RUN_ID)
    assert tombstone == {
        "schema_version": 2,
        "run_id": RUN_ID,
        "status": "purged",
        "created_at": tombstone["created_at"],
        "completed_at": tombstone["completed_at"],
    }
    view = load_run_view(paths, RUN_ID)
    assert view["phase"] == "missing"
    assert "purge" not in view

    replacement_task_run = "rr-fedcba9876543210"
    submit_job(paths, job(replacement_task_run))
    assert load_job(paths, replacement_task_run)[0]["task_id"] == "task-1"


def test_delete_artifacts_is_explicit_in_preview(tmp_path: Path) -> None:
    paths = failed_queue(tmp_path / "controller")

    preview = run_purge.purge_run(
        args(paths.root, apply=False, delete_artifacts=True)
    )
    assert preview["delete_artifacts"] is True


def test_execution_run_purge_stages_and_resumes_with_minimal_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = failed_execution(tmp_path / "controller")
    monkeypatch.setattr(
        output_sync,
        "invoke_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("source missing")),
    )
    sync_attempt = output_sync.process_pending_once(
        execution_paths,
        connect_timeout=8,
    )
    assert sync_attempt["retryable"] == 1

    target_attempts = 0

    def purge_target(
        _config: object,
        run_id: str,
        *,
        connect_timeout: int,
    ) -> dict[str, object]:
        nonlocal target_attempts
        assert run_id == RUN_ID
        assert connect_timeout == 8
        target_attempts += 1
        if target_attempts == 1:
            raise RuntimeError("archive cleanup interrupted")
        return {"ok": True, "action": "already_absent"}

    artifact_calls: list[str] = []
    worktree_calls: list[str] = []
    monkeypatch.setattr(output_sync, "_purge_target_run", purge_target)
    monkeypatch.setattr(
        run_purge,
        "purge_remote_run_artifacts",
        lambda **kwargs: (
            artifact_calls.append(str(kwargs["run_id"]))
            or {"ok": True, "action": "already_absent"}
        ),
    )
    monkeypatch.setattr(
        run_purge,
        "purge_remote_worktree",
        lambda **kwargs: (
            worktree_calls.append(str(kwargs["remote_workdir"]))
            or {"ok": True, "action": "removed"}
        ),
    )

    first = run_purge.purge_run(
        args(paths.root, apply=True, delete_artifacts=True)
    )

    assert first["status"] == "attention_required"
    assert first["failures"][0]["error"] == "archive cleanup interrupted"
    assert not (paths.queue_dir / RUN_ID).exists()
    assert not (execution_paths.runs_dir / RUN_ID).exists()
    records = paths.run_purges_dir / RUN_ID / "records"
    assert (records / "queue").is_dir()
    assert (records / "execution").is_dir()
    plan = run_purge._load_plan(paths, RUN_ID)
    assert plan is not None
    assert plan["progress"]["staged_queue"] is True
    assert plan["progress"]["staged_execution"] is True
    tombstone = load_run_tombstone(paths, RUN_ID)
    assert tombstone == {
        "schema_version": 2,
        "run_id": RUN_ID,
        "status": "purging",
        "created_at": tombstone["created_at"],
        "completed_at": None,
    }

    second = run_purge.purge_run(
        args(paths.root, apply=True, delete_artifacts=True)
    )

    assert second["status"] == "complete"
    assert target_attempts == 2
    assert artifact_calls == [RUN_ID]
    assert worktree_calls == ["/srv/worktrees/" + "a" * 40]
    assert output_sync.sync_status(execution_paths.registry_root) == {
        "enabled": True,
        "paused": False,
        "pending": 0,
        "completed": 0,
        "retryable": 0,
        "waiting": 0,
    }
    tombstone = load_run_tombstone(paths, RUN_ID)
    assert tombstone == {
        "schema_version": 2,
        "run_id": RUN_ID,
        "status": "purged",
        "created_at": tombstone["created_at"],
        "completed_at": tombstone["completed_at"],
    }
    assert load_run_view(paths, RUN_ID)["phase"] == "missing"


def test_execution_run_purge_rejects_frozen_task_mismatch(tmp_path: Path) -> None:
    paths, execution_paths = failed_execution(tmp_path / "controller")
    create_run_tombstone(paths, RUN_ID)

    with pytest.raises(ValueError, match="belongs to another task"):
        stage_failed_current_run(
            execution_paths,
            RUN_ID,
            task_id="task-2",
        )

    assert (execution_paths.runs_dir / RUN_ID / "manifest.yaml").is_file()
