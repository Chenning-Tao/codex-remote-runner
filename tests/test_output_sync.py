from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from remote_runner._internal import output_sync, output_sync_remote, registration
from remote_runner._internal.controller import output_sync_worker
from remote_runner._internal.controller.registry import controller_paths
from remote_runner._internal.execution_registry import (
    load_current_run,
    project_paths,
    update_current_state,
    write_yaml,
)


RUN_ID = "rr-0123456789abcdef"


def make_run(
    tmp_path: Path,
    *,
    output: bool = True,
    sync_enabled: bool = True,
) -> tuple[Path, object]:
    project = tmp_path / "project"
    project.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    config = project / ".remote-runner.yaml"
    write_yaml(config, {"controller_registry": True})
    registration.register(
        argparse.Namespace(
            project_config=config,
            label="sync-test",
            task_id="task-1",
            result_intent="candidate",
            result_tags={"campaign": "sync-test"},
            workload_class="standard",
            server="compute-b",
            ssh="compute-b",
            ssh_profile="intranet",
            configured_cores=8,
            minimum_cores=1,
            workers=None,
            command="true\n",
            remote_workdir=str(worktree),
            project_python=sys.executable,
            expected_revision="a" * 40,
            source_revision="a" * 40,
            prepared_servers=["compute-b"],
            submitted_command="true",
            worker_defaulted=False,
            require_clean_worktree=True,
            output_root=None,
            output_relpath=None,
            output_path="/srv/project/output/run" if output else None,
            output_metadata=json.dumps({"code": "bb90"}),
            privacy=None,
            run_id=RUN_ID,
        )
    )
    paths = project_paths(config)
    if sync_enabled:
        output_sync.store_config(paths.registry_root, sync_config().to_payload())
    return config, paths


def sync_config() -> output_sync.OutputSyncConfig:
    return output_sync.validate_config_payload(
        {
            "schema_version": 1,
            "target_server": "archive",
            "target_ssh": "archive",
            "target_root": "/srv/archive/scientific-v1",
            "target_python": "/opt/python3",
            "source_ssh_config": "/home/user/.ssh/output-sync.conf",
            "source_hosts": {"compute-b": "compute-b-int"},
            "retry_seconds": 60,
        }
    )


def mark_succeeded(paths: object) -> None:
    _manifest, state = load_current_run(paths, RUN_ID)  # type: ignore[arg-type]
    update_current_state(
        paths,  # type: ignore[arg-type]
        RUN_ID,
        int(state["revision"]),
        {
            "status": "succeeded",
            "finished_at": "2026-07-22T00:00:00Z",
            "exit_code": 0,
        },
    )


def test_succeeded_output_creates_one_immutable_pending_intent(tmp_path: Path) -> None:
    _config, paths = make_run(tmp_path)

    mark_succeeded(paths)

    pending = output_sync.list_pending(paths.registry_root)  # type: ignore[attr-defined]
    assert len(pending) == 1
    assert pending[0] == {
        "schema_version": 1,
        "run_id": RUN_ID,
        "source_server": "compute-b",
        "source_path": "/srv/project/output/run",
        "revision": "a" * 40,
            "task_id": "task-1",
            "label": "sync-test",
            "result_intent": "candidate",
            "result_tags": {"campaign": "sync-test"},
        "output_metadata": {"code": "bb90"},
        "succeeded_at": "2026-07-22T00:00:00Z",
        "state_revision": 1,
    }


def test_succeeded_run_without_output_path_does_not_enqueue(tmp_path: Path) -> None:
    _config, paths = make_run(tmp_path, output=False)

    mark_succeeded(paths)

    assert output_sync.list_pending(paths.registry_root) == []  # type: ignore[attr-defined]


def test_project_without_output_sync_does_not_create_outbox(tmp_path: Path) -> None:
    _config, paths = make_run(tmp_path, sync_enabled=False)

    mark_succeeded(paths)

    assert output_sync.list_pending(paths.registry_root) == []  # type: ignore[attr-defined]


def test_pending_sync_is_confirmed_only_after_archive_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, paths = make_run(tmp_path)
    mark_succeeded(paths)
    calls: list[str] = []

    def invoke(_config, intent, *, connect_timeout):
        calls.append(str(intent["run_id"]))
        assert connect_timeout == 8
        return {
            "schema_version": 1,
            "run_id": RUN_ID,
            "disposition": "copied_and_verified",
        }

    monkeypatch.setattr(output_sync, "invoke_target", invoke)

    result = output_sync.process_pending_once(paths, connect_timeout=8)

    assert result["archived"] == 1
    assert result["remaining"] == 0
    assert calls == [RUN_ID]
    status = output_sync.sync_status(paths.registry_root)  # type: ignore[attr-defined]
    assert status == {
        "enabled": True,
        "paused": False,
        "pending": 0,
        "completed": 1,
        "retryable": 0,
        "waiting": 0,
    }
    run_status = output_sync.run_sync_status(paths.registry_root, RUN_ID)  # type: ignore[attr-defined]
    assert run_status["status"] == "completed"
    assert run_status["receipt"]["disposition"] == "copied_and_verified"


def test_failed_archive_pull_remains_pending_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, paths = make_run(tmp_path)
    mark_succeeded(paths)
    monkeypatch.setattr(
        output_sync,
        "invoke_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unreachable")),
    )

    result = output_sync.process_pending_once(paths, connect_timeout=8)

    assert result["retryable"] == 1
    assert result["remaining"] == 1
    assert output_sync.sync_status(paths.registry_root)["retryable"] == 1  # type: ignore[attr-defined]
    run_status = output_sync.run_sync_status(paths.registry_root, RUN_ID)  # type: ignore[attr-defined]
    assert run_status["status"] == "retryable"
    assert run_status["attempts"] == 1
    assert run_status["last_error"] == "unreachable"


def test_paused_config_collects_pending_without_invoking_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, paths = make_run(tmp_path)
    paused = {**sync_config().to_payload(), "paused": True}
    output_sync.store_config(paths.registry_root, paused)  # type: ignore[attr-defined]
    mark_succeeded(paths)
    monkeypatch.setattr(
        output_sync,
        "invoke_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("paused sync must not contact archive")
        ),
    )

    result = output_sync.process_pending_once(paths, connect_timeout=8)

    assert result == {
        "enabled": True,
        "paused": True,
        "processed": 0,
        "remaining": 1,
        "results": [],
    }


def test_remote_rsync_command_is_a_pull_from_named_source() -> None:
    payload = output_sync_remote.validate_payload(
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "source_server": "compute-b",
            "source_host": "compute-b-int",
            "source_path": "/srv/output/run",
            "target_server": "archive",
            "target_root": "/srv/archive",
            "source_ssh_config": "/home/user/.ssh/sync.conf",
            "revision": "a" * 40,
            "task_id": "task-1",
            "label": "test",
            "output_metadata": {},
            "succeeded_at": "2026-07-22T00:00:00Z",
        }
    )

    command = output_sync_remote.build_rsync_command(
        payload,
        destination=Path("/srv/archive/.staging/rr.partial"),
        source_kind="directory",
        verify_only=False,
    )

    assert command[-2] == "compute-b-int:/srv/output/run/"
    assert command[-1] == "/srv/archive/.staging/rr.partial/"
    assert "--checksum" in command
    assert "--partial" in command
    assert "--protect-args" in command
    assert command[command.index("-e") + 1] == (
        "ssh -F /home/user/.ssh/sync.conf -o BatchMode=yes "
        "-o ServerAliveInterval=30 -o ServerAliveCountMax=3"
    )


def test_restricted_source_key_leaves_remote_path_visible() -> None:
    payload = output_sync_remote.validate_payload(
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "source_server": "compute-b",
            "source_host": "compute-b-int",
            "source_path": "/srv/output/run",
            "target_server": "archive",
            "target_root": "/srv/archive",
            "source_ssh_config": "/home/user/.ssh/sync.conf",
            "restricted_source_keys": True,
            "revision": "a" * 40,
            "task_id": "task-1",
            "label": "test",
            "output_metadata": {},
            "succeeded_at": "2026-07-22T00:00:00Z",
        }
    )

    command = output_sync_remote.build_rsync_command(
        payload,
        destination=Path("/srv/archive/.staging/rr.partial"),
        source_kind="directory",
        verify_only=False,
    )

    assert "--protect-args" not in command


def test_controller_host_starts_one_exact_output_sync_tmux_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    output_sync.store_config(paths.registry_root, sync_config().to_payload())
    output_sync.enqueue_succeeded_output(
        paths.registry_root,
        {
            "run_id": RUN_ID,
            "server": "compute-b",
            "output_path": "/srv/output/run",
            "source_revision": "a" * 40,
            "task_id": "task-1",
            "label": "test",
            "output_metadata": {},
        },
        state_revision=1,
        succeeded_at="2026-07-22T00:00:00Z",
    )
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append([str(item) for item in argv])
        return type("Result", (), {"returncode": 1 if len(calls) == 1 else 0, "stderr": ""})()

    monkeypatch.setattr(output_sync_worker, "resolve_tmux_executable", lambda: "tmux")
    monkeypatch.setattr(output_sync_worker.subprocess, "run", fake_run)

    started = output_sync_worker.ensure_output_sync_worker(
        paths,
        timeout=8,
        interval=60,
    )

    assert started is True
    assert calls[0] == [
        "tmux",
        "has-session",
        "-t",
        "=rr-output-sync-example",
    ]
    assert calls[1][0:6] == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "rr-output-sync-example",
        sys.executable,
    ]


def test_controller_host_does_not_start_worker_while_sync_is_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    output_sync.store_config(
        paths.registry_root,
        {**sync_config().to_payload(), "paused": True},
    )
    output_sync.enqueue_succeeded_output(
        paths.registry_root,
        {
            "run_id": RUN_ID,
            "server": "compute-b",
            "output_path": "/srv/output/run",
            "source_revision": "a" * 40,
            "task_id": "task-1",
            "label": "test",
            "output_metadata": {},
        },
        state_revision=1,
        succeeded_at="2026-07-22T00:00:00Z",
    )
    monkeypatch.setattr(
        output_sync_worker,
        "resolve_tmux_executable",
        lambda: (_ for _ in ()).throw(AssertionError("tmux must not be queried")),
    )

    assert (
        output_sync_worker.ensure_output_sync_worker(
            paths,
            timeout=8,
            interval=60,
        )
        is False
    )
