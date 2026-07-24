from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from remote_runner import cli
from remote_runner._internal import output_sync, registration, task_purge
from remote_runner._internal.controller import task_purge as controller_task_purge
from remote_runner._internal.controller.registry import (
    controller_paths,
    create_task_tombstone,
    list_queued,
    load_task_tombstone,
    submit_job,
    transition_queued_state,
)
from remote_runner._internal.execution_registry import (
    compact_run_events,
    project_paths,
    sha256_bytes,
    update_current_state,
    write_yaml,
)


RUN_ID = "rr-0123456789abcdef"


def queued_job(
    *,
    run_id: str = RUN_ID,
    task_id: str = "task-1",
    output_path: str | None = None,
) -> dict[str, object]:
    command = "python experiment.py"
    return {
        "run_id": run_id,
        "revision": "a" * 40,
        "label": "purge test",
        "task_id": task_id,
        "result_intent": "excluded",
        "result_tags": {},
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
        "output_metadata": {},
    }


def purge_args(root: Path, *, apply: bool, task: str = "task-1") -> argparse.Namespace:
    return argparse.Namespace(
        controller_root=root,
        project_id="example",
        task_id=task,
        reason="obsolete test task",
        apply=apply,
        timeout=8,
    )


def register_execution(paths: object, *, output_path: str | None = None) -> object:
    registration.register(
        argparse.Namespace(
            project_config=paths.config_path,  # type: ignore[attr-defined]
            label="purge test",
            task_id="task-1",
            result_intent="excluded",
            result_tags={},
            workload_class="standard",
            server="compute-a",
            ssh="compute-a",
            ssh_profile="test",
            configured_cores=8,
            minimum_cores=1,
            workers=1,
            command="true",
            remote_workdir="/srv/example/worktrees/" + "a" * 40,
            project_python=sys.executable,
            expected_revision="a" * 40,
            source_revision="a" * 40,
            prepared_servers=["compute-a"],
            submitted_command="true",
            worker_defaulted=False,
            require_clean_worktree=True,
            output_root=None,
            output_relpath=None,
            output_path=output_path,
            output_metadata=None,
            privacy=None,
            run_id=RUN_ID,
        )
    )
    return project_paths(paths.config_path)  # type: ignore[attr-defined]


def test_cli_exposes_task_purge_as_dry_run_by_default() -> None:
    args = cli.build_parser().parse_args(["purge-task", "--task-id", "task-1"])

    assert args.apply is False
    assert args.reason == "user confirmed this task is no longer needed"


def test_embedded_purge_programs_are_valid_python() -> None:
    compile(task_purge.PURGE_RUN_PROGRAM, "<purge-run>", "exec")
    compile(task_purge.PURGE_WORKTREE_PROGRAM, "<purge-worktree>", "exec")
    compile(output_sync.PURGE_TARGET_PROGRAM, "<purge-target>", "exec")


def test_task_purge_uses_exact_stored_identity(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(paths, queued_job(task_id="archive/tasks/task-1"))

    with pytest.raises(ValueError, match="exact stored"):
        controller_task_purge.purge_task(purge_args(paths.root, apply=False))

    preview = controller_task_purge.purge_task(
        purge_args(paths.root, apply=False, task="archive/tasks/task-1")
    )
    assert preview["status"] == "ready"
    assert preview["candidate_count"] == 1


def test_queue_only_task_is_stopped_purged_and_tombstoned(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(paths, queued_job())

    result = controller_task_purge.purge_task(purge_args(paths.root, apply=True))

    assert result["status"] == "complete"
    assert result["run_ids"] == [RUN_ID]
    assert not (paths.queue_dir / RUN_ID).exists()
    tombstone = load_task_tombstone(paths, "task-1")
    assert tombstone is not None
    assert tombstone["status"] == "purged"
    with pytest.raises(ValueError, match="cannot accept new runs"):
        submit_job(paths, queued_job(run_id="rr-fedcba9876543210"))


def test_tombstone_closes_dispatch_race(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())
    create_task_tombstone(paths, "task-1", reason="obsolete")

    assert list_queued(paths) == []
    with pytest.raises(RuntimeError, match="tombstoned"):
        transition_queued_state(
            paths,
            RUN_ID,
            expected_revision=0,
            status="dispatching",
        )


def test_dispatched_queue_without_execution_is_blocked(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(paths, queued_job())
    state = transition_queued_state(
        paths, RUN_ID, expected_revision=0, status="dispatching"
    )
    transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=int(state["revision"]),
        status="dispatched",
    )

    result = controller_task_purge.purge_task(purge_args(paths.root, apply=False))

    assert result["status"] == "blocked"
    assert "no execution authority" in result["blockers"][0]["error"]


def test_failed_execution_purges_remote_resources_and_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(paths, queued_job(output_path="/srv/example/output/task-1"))
    state = transition_queued_state(
        paths, RUN_ID, expected_revision=0, status="dispatching"
    )
    transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=int(state["revision"]),
        status="failed",
    )
    execution_paths = register_execution(
        paths,
        output_path="/srv/example/output/task-1",
    )
    update_current_state(
        execution_paths,  # type: ignore[arg-type]
        RUN_ID,
        0,
        {"status": "failed", "finished_at": "2026-01-01T00:00:00Z"},
    )
    artifact_calls: list[str] = []
    worktree_calls: list[str] = []
    monkeypatch.setattr(
        controller_task_purge,
        "purge_remote_run_artifacts",
        lambda **kwargs: (
            artifact_calls.append(str(kwargs["run_id"]))
            or {"ok": True, "action": "purged"}
        ),
    )
    monkeypatch.setattr(
        controller_task_purge,
        "purge_remote_worktree",
        lambda **kwargs: (
            worktree_calls.append(str(kwargs["remote_workdir"]))
            or {"ok": True, "action": "removed"}
        ),
    )

    result = controller_task_purge.purge_task(purge_args(paths.root, apply=True))

    assert result["status"] == "complete"
    assert artifact_calls == [RUN_ID]
    assert worktree_calls == ["/srv/example/worktrees/" + "a" * 40]
    assert not (execution_paths.runs_dir / RUN_ID).exists()  # type: ignore[attr-defined]
    assert RUN_ID not in execution_paths.events_path.read_text(  # type: ignore[attr-defined]
        encoding="utf-8"
    )


def test_task_purge_resumes_after_unknown_remote_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(paths, queued_job())
    state = transition_queued_state(
        paths, RUN_ID, expected_revision=0, status="dispatching"
    )
    transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=int(state["revision"]),
        status="failed",
    )
    execution_paths = register_execution(paths)
    update_current_state(
        execution_paths,  # type: ignore[arg-type]
        RUN_ID,
        0,
        {"status": "failed"},
    )
    attempts = 0

    def purge_artifacts(**_kwargs: object) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise task_purge.PurgeOutcomeUnknown("transport outcome unknown")
        return {"ok": True, "action": "already_absent"}

    monkeypatch.setattr(
        controller_task_purge,
        "purge_remote_run_artifacts",
        purge_artifacts,
    )
    monkeypatch.setattr(
        controller_task_purge,
        "purge_remote_worktree",
        lambda **_kwargs: {"ok": True, "action": "removed"},
    )

    first = controller_task_purge.purge_task(purge_args(paths.root, apply=True))
    assert first["status"] == "attention_required"
    assert not (execution_paths.runs_dir / RUN_ID).exists()  # type: ignore[attr-defined]
    assert controller_task_purge._plan_path(paths, "task-1").is_file()

    second = controller_task_purge.purge_task(purge_args(paths.root, apply=True))

    assert second["status"] == "complete"
    assert attempts == 2


def test_event_compaction_fails_closed_on_malformed_matching_record(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".remote-runner.yaml"
    write_yaml(config, {"controller_registry": True})
    paths = project_paths(config)
    paths.registry_root.mkdir(exist_ok=True)
    original = f'{{"run_id":"{RUN_ID}"\n'.encode()
    paths.events_path.write_bytes(original)

    with pytest.raises(ValueError, match="malformed"):
        compact_run_events(paths, {RUN_ID})

    assert paths.events_path.read_bytes() == original


def test_task_purge_blocks_output_overlapping_a_retained_run(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(paths, queued_job(output_path="/srv/example/output"))
    retained = queued_job(
        run_id="rr-fedcba9876543210",
        task_id="task-2",
        output_path="/srv/example/output/task-2",
    )
    submit_job(paths, retained)
    state = transition_queued_state(
        paths, RUN_ID, expected_revision=0, status="dispatching"
    )
    transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=int(state["revision"]),
        status="failed",
    )
    execution_paths = register_execution(paths, output_path="/srv/example/output")
    update_current_state(
        execution_paths,  # type: ignore[arg-type]
        RUN_ID,
        0,
        {"status": "failed"},
    )

    preview = controller_task_purge.purge_task(purge_args(paths.root, apply=False))

    assert preview["status"] == "blocked"
    assert preview["blockers"][0]["retained_run_id"] == "rr-fedcba9876543210"


def test_purge_run_program_removes_failed_runtime_and_output(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = home / ".rr" / RUN_ID
    runtime.mkdir(parents=True)
    (runtime / "status.json").write_text(
        json.dumps({"run_id": RUN_ID, "state": "failed"}),
        encoding="utf-8",
    )
    output_root = tmp_path / "output"
    output = output_root / RUN_ID
    output.mkdir(parents=True)
    (output / "result.txt").write_text("obsolete", encoding="utf-8")
    workdir = tmp_path / "worktrees" / ("a" * 40)
    workdir.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tmux = bindir / "tmux"
    tmux.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    tmux.chmod(0o755)
    payload = {
        "run_id": RUN_ID,
        "expected_state": "failed",
        "remote_workdir": str(workdir),
        "output_root": str(output_root),
        "output_path": str(output),
    }
    env = dict(os.environ)
    env.update({"HOME": str(home), "PATH": f"{bindir}:{env['PATH']}"})

    completed = subprocess.run(
        [sys.executable, "-"],
        input=task_purge._stdin(payload, task_purge.PURGE_RUN_PROGRAM),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    assert not runtime.exists()
    assert not output.exists()


def test_output_sync_purge_serializes_target_and_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry_root = tmp_path / "registry"
    output_sync.store_config(
        registry_root,
        {
            "schema_version": 1,
            "target_server": "archive",
            "target_ssh": "archive",
            "target_root": "/srv/archive",
            "target_python": "/opt/python3",
            "source_ssh_config": "/home/user/.ssh/sync.conf",
            "source_hosts": {},
            "retry_seconds": 60,
            "paused": False,
        },
    )
    paths = output_sync.output_sync_paths(registry_root)
    for directory in (paths.pending_dir, paths.completed_dir, paths.state_dir):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{RUN_ID}.json").write_text("{}\n", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        output_sync,
        "_purge_target_run",
        lambda _config, run_id, *, connect_timeout: (
            calls.append(run_id) or {"ok": True}
        ),
    )

    result = output_sync.purge_run_sync_state(
        registry_root,
        {RUN_ID},
        target_configs={RUN_ID: output_sync.load_config(registry_root).to_payload()},  # type: ignore[union-attr]
        connect_timeout=8,
    )

    assert calls == [RUN_ID]
    assert result[0]["removed_controller_state"] == ["pending", "completed", "state"]
    assert not any(
        (directory / f"{RUN_ID}.json").exists()
        for directory in (
            paths.pending_dir,
            paths.completed_dir,
            paths.state_dir,
        )
    )
