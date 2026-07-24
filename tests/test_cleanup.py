from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from remote_runner._internal import cleanup, registration
from remote_runner._internal.controller import service as controller_service
from remote_runner._internal.controller.registry import (
    controller_paths,
    load_job,
    submit_job,
    transition_queued_state,
)
from remote_runner._internal.execution_registry import (
    project_paths,
    sha256_bytes,
    update_current_state,
    write_yaml,
)


RUN_ID = "rr-0123456789abcdef"


def queued_job() -> dict[str, object]:
    command = "python experiment.py"
    return {
        "run_id": RUN_ID,
        "revision": "a" * 40,
        "label": "cleanup test",
        "task_id": "task-1",
        "result_intent": "candidate",
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
                "bare_repo": "/srv/repo.git",
                "worktree_root": "/srv/worktrees",
                "python": sys.executable,
                "output_root": None,
            }
        ],
        "output_relpath": None,
        "output_path": None,
        "output_metadata": {},
    }


def cleanup_args(root: Path, *, apply: bool) -> argparse.Namespace:
    return argparse.Namespace(
        controller_root=root,
        project_id="example",
        run_id=None,
        apply=apply,
        timeout=8,
    )


def install_fake_tmux(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tmux = bindir / "tmux"
    tmux.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    tmux.chmod(0o755)
    return bindir


def run_cleanup_program(
    tmp_path: Path, state: str
) -> subprocess.CompletedProcess[bytes]:
    home = tmp_path / "home"
    runtime = home / ".rr" / RUN_ID
    runtime.mkdir(parents=True)
    (runtime / "status.json").write_text(
        json.dumps({"run_id": RUN_ID, "state": state}),
        encoding="utf-8",
    )
    (runtime / "log").write_text("partial output", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {"HOME": str(home), "PATH": f"{install_fake_tmux(tmp_path)}:{env['PATH']}"}
    )
    return subprocess.run(
        [sys.executable, "-"],
        input=cleanup.build_cleanup_stdin(RUN_ID),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )


def test_remote_cleanup_removes_only_stopped_runtime(tmp_path: Path) -> None:
    completed = run_cleanup_program(tmp_path, "stopped")

    assert completed.returncode == 0, completed.stderr.decode()
    assert not (tmp_path / "home" / ".rr" / RUN_ID).exists()
    assert cleanup._cleanup_result(completed.stdout) == {
        "action": "removed",
        "message": None,
        "ok": True,
    }


def test_remote_cleanup_refuses_succeeded_runtime(tmp_path: Path) -> None:
    completed = run_cleanup_program(tmp_path, "succeeded")

    assert completed.returncode != 0
    assert (tmp_path / "home" / ".rr" / RUN_ID).is_dir()
    assert cleanup._cleanup_result(completed.stdout) == {
        "action": "validation",
        "message": "runtime status is not stopped",
        "ok": False,
    }


def test_cleanup_defaults_to_dry_run_then_purges_stopped_queue(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())
    transition_queued_state(paths, RUN_ID, expected_revision=0, status="stopped")

    dry_run = controller_service.cleanup_records(cleanup_args(paths.root, apply=False))

    assert dry_run["candidate_count"] == 1
    assert dry_run["candidates"][0]["queue_status"] == "stopped"
    assert load_job(paths, RUN_ID)[1]["status"] == "stopped"

    applied = controller_service.cleanup_records(cleanup_args(paths.root, apply=True))

    assert applied["purged_count"] == 1
    assert applied["failed_count"] == 0
    assert not (paths.queue_dir / RUN_ID).exists()


def test_cleanup_prunes_runtime_before_purging_stopped_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
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
    write_yaml(paths.config_path, {"controller_registry": True})
    registration.register(
        argparse.Namespace(
            project_config=paths.config_path,
            label="cleanup test",
            task_id="task-1",
            server="compute-a",
            ssh="compute-a",
            ssh_profile="test",
            configured_cores=8,
            minimum_cores=1,
            workers=1,
            command="true",
            remote_workdir="/srv/worktrees/" + "a" * 40,
            project_python=sys.executable,
            expected_revision=None,
            require_clean_worktree=False,
            output_path=None,
            output_metadata=None,
            run_id=RUN_ID,
        )
    )
    execution_paths = project_paths(paths.config_path)
    update_current_state(
        execution_paths,
        RUN_ID,
        0,
        {"status": "stopped", "finished_at": "2026-01-01T00:00:00+00:00"},
    )
    calls: list[str] = []
    monkeypatch.setattr(
        controller_service,
        "cleanup_remote_runtime",
        lambda _ssh, _python, run_id, _timeout: (
            calls.append(run_id) or {"ok": True, "action": "removed"}
        ),
    )

    result = controller_service.cleanup_records(cleanup_args(paths.root, apply=True))

    assert result["purged_count"] == 1
    assert result["failed_count"] == 0
    assert calls == [RUN_ID]
    assert not (paths.queue_dir / RUN_ID).exists()
    assert not (execution_paths.runs_dir / RUN_ID).exists()


def test_cleanup_preserves_controller_records_when_runtime_verification_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
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
    write_yaml(paths.config_path, {"controller_registry": True})
    registration.register(
        argparse.Namespace(
            project_config=paths.config_path,
            label="cleanup test",
            task_id="task-1",
            server="compute-a",
            ssh="compute-a",
            ssh_profile="test",
            configured_cores=8,
            minimum_cores=1,
            workers=1,
            command="true",
            remote_workdir="/srv/worktrees/" + "a" * 40,
            project_python=sys.executable,
            expected_revision=None,
            require_clean_worktree=False,
            output_path=None,
            output_metadata=None,
            run_id=RUN_ID,
        )
    )
    execution_paths = project_paths(paths.config_path)
    update_current_state(execution_paths, RUN_ID, 0, {"status": "stopped"})

    def fail_cleanup(*_args) -> dict[str, object]:
        raise cleanup.CleanupOutcomeUnknown("server unreachable")

    monkeypatch.setattr(controller_service, "cleanup_remote_runtime", fail_cleanup)

    result = controller_service.cleanup_records(cleanup_args(paths.root, apply=True))

    assert result["purged_count"] == 0
    assert result["failed_count"] == 1
    assert (paths.queue_dir / RUN_ID / "job.yaml").is_file()
    assert (execution_paths.runs_dir / RUN_ID / "manifest.yaml").is_file()


def test_cleanup_does_not_select_failed_or_succeeded_records(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())
    transition_queued_state(paths, RUN_ID, expected_revision=0, status="failed")

    result = controller_service.cleanup_records(cleanup_args(paths.root, apply=False))

    assert result == {"applied": False, "candidate_count": 0, "candidates": []}
