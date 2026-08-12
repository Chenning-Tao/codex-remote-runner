from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import pytest

from remote_runner import cli
from remote_runner._internal import decommissioned_run, output_sync, registration
from remote_runner._internal.controller import service as controller_service
from remote_runner._internal.controller.registry import (
    ControllerPaths,
    MalformedLeaseError,
    controller_paths,
    controller_scheduler_paths,
    load_job,
    submit_job,
    transition_queued_state,
)
from remote_runner._internal.controller.run_view import load_run_view
from remote_runner._internal.execution_registry import (
    ProjectPaths,
    load_current_run,
    project_paths,
    sha256_bytes,
    update_current_state,
    write_yaml,
)


RUN_ID = "rr-0123456789abcdef"


def queued_job(*, output_path: str | None = None) -> dict[str, object]:
    command = "python workload.py"
    return {
        "run_id": RUN_ID,
        "revision": "a" * 40,
        "label": "decommissioned attempt",
        "task_id": "task-1",
        "submitted_command": command,
        "submitted_command_sha256": sha256_bytes(command.encode()),
        "prepared_servers": [
            {
                "name": "retired-a",
                "ssh": "retired-a",
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
        "output_sync": None,
        "lease_seconds": 120,
    }


def running_execution(
    root: Path,
    *,
    output_path: str | None = None,
    start_running: bool = True,
) -> tuple[ControllerPaths, ProjectPaths]:
    paths = controller_paths(root, "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    submit_job(paths, queued_job(output_path=output_path))
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
            label="decommissioned attempt",
            task_id="task-1",
            workload_class="standard",
            server="retired-a",
            ssh="retired-a",
            ssh_profile="auto",
            configured_cores=8,
            minimum_cores=1,
            assigned_cores=8,
            command="python workload.py",
            remote_workdir="/srv/worktrees/" + "a" * 40,
            project_python=sys.executable,
            expected_revision="a" * 40,
            source_revision="a" * 40,
            prepared_servers=["retired-a"],
            submitted_command="python workload.py",
            require_clean_worktree=True,
            output_root=None,
            output_relpath=None,
            output_path=output_path,
            output_metadata=None,
            privacy=None,
            run_id=RUN_ID,
        )
    )
    execution_paths = project_paths(paths.config_path)
    if start_running:
        _manifest, current = load_current_run(execution_paths, RUN_ID)
        update_current_state(
            execution_paths,
            RUN_ID,
            int(current["revision"]),
            {"status": "running", "started_at": "2026-08-01T00:00:00Z"},
        )
    return paths, execution_paths


def unreachable_probe(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {
        "observation": "unreachable",
        "ssh_reachable": False,
        "error": "connection timed out",
    }


def close(
    paths: ControllerPaths,
    *,
    apply: bool,
) -> dict[str, object]:
    return decommissioned_run.inspect_or_close(
        paths,
        RUN_ID,
        server="retired-a",
        reason="operator confirmed the physical server was destroyed",
        timeout=8,
        apply=apply,
    )


def test_decommissioned_run_is_preview_first_and_preserves_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = running_execution(tmp_path / "controller")
    monkeypatch.setattr(decommissioned_run.monitoring, "remote_probe", unreachable_probe)

    preview = close(paths, apply=False)

    assert preview["status"] == "ready_to_close"
    assert preview["applied"] is False
    assert preview["probe"]["observation"] == "unreachable"
    assert load_current_run(execution_paths, RUN_ID)[1]["status"] == "running"

    applied = close(paths, apply=True)

    assert applied["status"] == "closed"
    assert applied["applied"] is True
    state = load_current_run(execution_paths, RUN_ID)[1]
    assert state["status"] == "stopped"
    assert state["exit_code"] is None
    assert state["error"] == (
        "server decommissioned: operator confirmed the physical server was destroyed"
    )
    assert load_job(paths, RUN_ID)[1]["status"] == "dispatched"
    assert load_run_view(paths, RUN_ID)["phase"] == "terminal"
    assert close(paths, apply=True)["status"] == "already_terminal"


def test_decommissioned_run_closes_registered_launch_with_unknown_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = running_execution(
        tmp_path / "controller",
        start_running=False,
    )
    monkeypatch.setattr(decommissioned_run.monitoring, "remote_probe", unreachable_probe)

    result = close(paths, apply=True)

    assert result["authoritative_status"] == "registered"
    state = load_current_run(execution_paths, RUN_ID)[1]
    assert state["status"] == "stopped"
    assert state["started_at"] is None


@pytest.mark.parametrize(
    "probe",
    [
        {"observation": "registered", "ssh_reachable": True},
        {"observation": "unknown", "ssh_reachable": True},
        {"observation": "running", "ssh_reachable": True, "pgid_alive": True},
        {"observation": "unknown", "ssh_reachable": False},
    ],
)
def test_decommissioned_run_requires_fresh_unreachable_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: dict[str, object],
) -> None:
    paths, execution_paths = running_execution(tmp_path / "controller")
    monkeypatch.setattr(
        decommissioned_run.monitoring,
        "remote_probe",
        lambda *_args, **_kwargs: probe,
    )

    with pytest.raises(RuntimeError, match="not proven unreachable"):
        close(paths, apply=True)

    assert load_current_run(execution_paths, RUN_ID)[1]["status"] == "running"


def test_decommissioned_run_rejects_server_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = running_execution(tmp_path / "controller")
    monkeypatch.setattr(decommissioned_run.monitoring, "remote_probe", unreachable_probe)

    with pytest.raises(ValueError, match="belongs to server"):
        decommissioned_run.inspect_or_close(
            paths,
            RUN_ID,
            server="retired-b",
            reason="destroyed",
            timeout=8,
            apply=True,
        )

    assert load_current_run(execution_paths, RUN_ID)[1]["status"] == "running"


def test_decommissioned_run_rejects_malformed_or_active_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = running_execution(tmp_path / "controller")
    monkeypatch.setattr(decommissioned_run.monitoring, "remote_probe", unreachable_probe)
    scheduler = controller_scheduler_paths(paths.root)
    scheduler.leases_dir.mkdir(parents=True)
    lease_path = scheduler.leases_dir / "retired-a.yaml"
    lease_path.write_text("expires_at: [broken\n", encoding="utf-8")

    with pytest.raises(MalformedLeaseError, match="malformed dispatch lease"):
        close(paths, apply=True)
    assert load_current_run(execution_paths, RUN_ID)[1]["status"] == "running"

    write_yaml(
        lease_path,
        {
            "schema_version": 1,
            "kind": "dispatch",
            "machine_id": "retired-a",
            "server": "retired-a",
            "project_id": "example",
            "run_id": RUN_ID,
            "created_at": 1000.0,
            "expires_at": 1001.0,
        },
    )
    with pytest.raises(RuntimeError, match="active controller lease"):
        close(paths, apply=True)
    assert load_current_run(execution_paths, RUN_ID)[1]["status"] == "running"


def test_decommissioned_run_rechecks_lease_immediately_before_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = running_execution(tmp_path / "controller")
    monkeypatch.setattr(decommissioned_run.monitoring, "remote_probe", unreachable_probe)
    calls = 0

    def leases(_paths: ControllerPaths) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return []
        return [
            {
                "kind": "dispatch",
                "machine_id": "retired-a",
                "server": "retired-a",
                "project_id": "example",
                "run_id": "rr-fedcba9876543210",
            }
        ]

    monkeypatch.setattr(decommissioned_run, "_leases", leases)

    with pytest.raises(RuntimeError, match="acquired a controller lease"):
        close(paths, apply=True)

    assert calls == 2
    assert load_current_run(execution_paths, RUN_ID)[1]["status"] == "running"


def test_decommissioned_run_blocks_configured_output_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, execution_paths = running_execution(
        tmp_path / "controller",
        output_path="/srv/output/run",
    )
    output_sync.store_config(
        execution_paths.registry_root,
        {
            "schema_version": 1,
            "target_server": "archive",
            "target_ssh": "archive",
            "target_root": "/srv/archive/project",
            "target_python": sys.executable,
            "source_ssh_config": "/srv/archive/ssh.conf",
            "source_hosts": {"retired-a": "retired-a-int"},
            "retry_seconds": 60,
            "paused": False,
        },
    )
    monkeypatch.setattr(decommissioned_run.monitoring, "remote_probe", unreachable_probe)

    with pytest.raises(RuntimeError, match="configured output synchronization"):
        close(paths, apply=True)

    assert load_current_run(execution_paths, RUN_ID)[1]["status"] == "running"


def test_decommissioned_run_cli_sends_explicit_preview_and_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / ".remote-runner.yaml"
    write_yaml(
        config,
        {
            "controller": {
                "ssh": "controller_host",
                "root": "/Users/test/.remote-runner",
            },
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
    observed: list[dict[str, object]] = []

    def call(_config, action: str, **kwargs: object) -> dict[str, object]:
        observed.append({"action": action, **kwargs})
        return {"status": "ready_to_close"}

    monkeypatch.setattr(decommissioned_run, "call_controller", call)
    base = [
        "close-decommissioned-run",
        "--project-config",
        str(config),
        "--run-id",
        RUN_ID,
        "--server",
        "retired-a",
        "--reason",
        "destroyed by provider",
        "--timeout",
        "4",
    ]

    assert cli.main(base) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready_to_close"
    assert cli.main([*base, "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready_to_close"

    assert observed == [
        {
            "action": "close-decommissioned-run",
            "timeout": 4,
            "action_args": ("--run-id", RUN_ID),
            "payload": {
                "schema_version": 1,
                "server": "retired-a",
                "reason": "destroyed by provider",
            },
        },
        {
            "action": "close-decommissioned-run",
            "timeout": 4,
            "action_args": ("--run-id", RUN_ID, "--apply"),
            "payload": {
                "schema_version": 1,
                "server": "retired-a",
                "reason": "destroyed by provider",
            },
        },
    ]


def test_controller_decommissioned_action_validates_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def inspect(paths: ControllerPaths, run_id: str, **kwargs: object) -> dict[str, object]:
        observed.update(paths=paths, run_id=run_id, **kwargs)
        return {"status": "ready_to_close"}

    monkeypatch.setattr(controller_service, "inspect_or_close", inspect)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "schema_version": 1,
                    "server": "retired-a",
                    "reason": "provider destroyed the instance",
                }
            )
        ),
    )
    result = controller_service.close_decommissioned_run(
        argparse.Namespace(
            controller_root=tmp_path / "controller",
            project_id="example",
            run_id=RUN_ID,
            timeout=7,
            apply=False,
        )
    )

    assert result == {"status": "ready_to_close"}
    assert observed["run_id"] == RUN_ID
    assert observed["server"] == "retired-a"
    assert observed["reason"] == "provider destroyed the instance"
    assert observed["timeout"] == 7
    assert observed["apply"] is False
