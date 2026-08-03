from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from remote_runner._internal.controller import run_purge
from remote_runner._internal.controller.registry import (
    controller_paths,
    load_job,
    load_run_tombstone,
    submit_job,
    transition_queued_state,
)
from remote_runner._internal.controller.run_view import load_run_view
from remote_runner._internal.execution_registry import sha256_bytes


RUN_ID = "rr-0123456789abcdef"


def job(run_id: str = RUN_ID) -> dict[str, object]:
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
        "output_path": None,
        "output_metadata": {},
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
