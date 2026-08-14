from __future__ import annotations

from pathlib import Path

import pytest

from remote_runner._internal.controller import dispatcher
from remote_runner._internal.controller.registry import (
    acquire_dispatch_lease,
    controller_paths,
    controller_scheduler_paths,
    dispatch_lease_authority_gone,
    load_yaml,
    release_dispatch_lease,
    submit_job,
)
from remote_runner._internal.execution_registry import sha256_bytes, write_yaml


RUN_ID = "rr-0123456789abcdef"
OTHER_RUN_ID = "rr-fedcba9876543210"


def queued_job(run_id: str = RUN_ID) -> dict[str, object]:
    command = "python experiment.py"
    return {
        "run_id": run_id,
        "revision": "a" * 40,
        "label": "experiment",
        "task_id": "task-1",
        "submitted_command": command,
        "submitted_command_sha256": sha256_bytes(command.encode()),
        "prepared_servers": [
            {
                "name": "compute-a",
                "ssh": "compute-a",
                "ssh_profile": "intranet",
                "configured_cores": 256,
                "priority": 100,
                "bare_repo": "/srv/example/repo.git",
                "worktree_root": "/srv/example/worktrees",
                "python": "/opt/example/bin/python3",
                "output_root": None,
            }
        ],
        "output_relpath": None,
        "output_path": None,
        "output_metadata": {},
    }


def write_execution_record(root: Path, project_id: str, run_id: str) -> None:
    """Create a minimal current-format execution record without full validation."""
    project = controller_paths(root, project_id)
    project.project_root.mkdir(parents=True, exist_ok=True)
    write_yaml(project.config_path, {"controller_registry": True})
    runs_dir = project.registry_root / "runs"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    write_yaml(run_dir / "manifest.yaml", {"schema_version": 5})


def test_authority_gone_rule_requires_both_records_absent(tmp_path: Path) -> None:
    root = tmp_path / "controller"
    paths = controller_paths(root, "project-a")

    assert dispatch_lease_authority_gone(
        paths, project_id="project-a", run_id=RUN_ID
    )

    submit_job(paths, queued_job())
    assert not dispatch_lease_authority_gone(
        paths, project_id="project-a", run_id=RUN_ID
    )


def test_expired_foreign_dispatch_lease_is_cleaned_when_records_are_gone(
    tmp_path: Path,
) -> None:
    root = tmp_path / "controller"
    first = controller_paths(root, "project-a")
    second = controller_paths(root, "project-b")

    ownership = acquire_dispatch_lease(
        first,
        server="compute-a",
        run_id=RUN_ID,
        ttl_seconds=120,
        now=1000,
    )
    assert ownership is not None

    fenced = acquire_dispatch_lease(
        second,
        server="compute-a",
        run_id=OTHER_RUN_ID,
        ttl_seconds=120,
        now=1121,
    )
    assert fenced is not None
    lease = load_yaml(controller_scheduler_paths(root).leases_dir / "compute-a.yaml")
    assert lease["project_id"] == "project-b"
    assert lease["run_id"] == OTHER_RUN_ID


def test_expired_foreign_dispatch_lease_still_blocks_with_queue_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "controller"
    first = controller_paths(root, "project-a")
    second = controller_paths(root, "project-b")

    submit_job(first, queued_job())
    ownership = acquire_dispatch_lease(
        first,
        server="compute-a",
        run_id=RUN_ID,
        ttl_seconds=120,
        now=1000,
    )
    assert ownership is not None
    assert not acquire_dispatch_lease(
        second,
        server="compute-a",
        run_id=OTHER_RUN_ID,
        ttl_seconds=120,
        now=1121,
    )


def test_expired_foreign_dispatch_lease_still_blocks_with_execution_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "controller"
    first = controller_paths(root, "project-a")
    second = controller_paths(root, "project-b")

    write_execution_record(root, "project-a", RUN_ID)
    ownership = acquire_dispatch_lease(
        first,
        server="compute-a",
        run_id=RUN_ID,
        ttl_seconds=120,
        now=1000,
    )
    assert ownership is not None
    assert not acquire_dispatch_lease(
        second,
        server="compute-a",
        run_id=OTHER_RUN_ID,
        ttl_seconds=120,
        now=1121,
    )


def test_owner_reconciliation_releases_orphaned_lease_without_records(
    tmp_path: Path,
) -> None:
    root = tmp_path / "controller"
    paths = controller_paths(root, "project-a")

    ownership = acquire_dispatch_lease(
        paths,
        server="compute-a",
        run_id=RUN_ID,
        ttl_seconds=120,
        now=1000,
    )
    assert ownership is not None

    dispatcher._reconcile_owned_dispatch_leases(paths, timeout=1)

    lease_path = controller_scheduler_paths(root).leases_dir / "compute-a.yaml"
    assert not lease_path.exists()


def test_owner_reconciliation_keeps_orphaned_lease_with_queue_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "controller"
    paths = controller_paths(root, "project-a")

    submit_job(paths, queued_job())
    ownership = acquire_dispatch_lease(
        paths,
        server="compute-a",
        run_id=RUN_ID,
        ttl_seconds=120,
        now=1000,
    )
    assert ownership is not None

    dispatcher._reconcile_owned_dispatch_leases(paths, timeout=1)

    lease_path = controller_scheduler_paths(root).leases_dir / "compute-a.yaml"
    assert lease_path.is_file()
    lease = load_yaml(lease_path)
    assert lease["run_id"] == RUN_ID


def test_release_dispatch_lease_of_missing_owner_is_a_noop(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "project-a")
    assert not release_dispatch_lease(paths, server="compute-a", run_id=RUN_ID)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
