from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from remote_runner._internal import codex_app_server, wakeup, wakeup_worker
from remote_runner._internal.execution_registry import write_yaml


RUN_ID = "rr-0123456789abcdef"
OTHER_RUN_ID = "rr-fedcba9876543210"
THREAD_ID = "019f93a3-2a16-7640-bd71-44aee4cc0fb2"


def config(tmp_path: Path) -> Path:
    path = tmp_path / ".remote-runner.yaml"
    write_yaml(
        path,
        {
            "project_id": "example",
            "controller": {"ssh": "controller_host", "root": "/srv/controller"},
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
    return path


def register_args(
    tmp_path: Path,
    config_path: Path,
    *,
    run_ids: list[str] | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        project_config=config_path,
        run_ids=run_ids or [RUN_ID],
        codex_thread_id=THREAD_ID,
        codex_executable=None,
        timeout=8,
        state_root=tmp_path / "wakeup-state",
    )


def stub_registration(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str]]:
    preflighted: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        wakeup,
        "resolve_codex_executable",
        lambda _value: Path("/opt/homebrew/bin/codex"),
    )
    monkeypatch.setattr(
        wakeup,
        "preflight_thread",
        lambda executable, thread_id: preflighted.append((executable, thread_id)),
    )
    monkeypatch.setattr(
        wakeup,
        "_initial_cohort_views",
        lambda _config, run_ids, **_kwargs: [
            run_view(
                run_id,
                "running",
                etag_character=chr(ord("a") + index % 26),
            )
            for index, run_id in enumerate(run_ids)
        ],
    )
    monkeypatch.setattr(
        wakeup,
        "start_worker",
        lambda _paths: {"started": True, "pid": 123},
    )
    monkeypatch.setattr(
        wakeup,
        "_supervisor_status",
        lambda _paths: {"available": True, "installed": False, "loaded": False},
    )
    return preflighted


def run_view(
    run_id: str,
    phase: str,
    *,
    outcome: str | None = None,
    etag_character: str = "a",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": "example",
        "run_id": run_id,
        "etag": "sha256:" + etag_character * 64,
        "phase": phase,
        "outcome": outcome,
        "terminal_source": "execution" if outcome else None,
        "attention_reason": (
            "queue status is unsupported" if phase == "attention_required" else None
        ),
        "queue": {"error": "must not reach the wake prompt"},
        "execution": {"error": "must not reach the wake prompt"},
        "output_sync": {"status": "not_enqueued", "last_error": "secret"},
        "purge": None,
    }


def test_register_is_durable_private_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preflighted = stub_registration(monkeypatch)
    args = register_args(tmp_path, config(tmp_path), run_ids=[OTHER_RUN_ID, RUN_ID])

    first = wakeup.register(args)
    second = wakeup.register(args)

    assert first["created"] is True
    assert second["created"] is False
    assert first["wake_id"] == second["wake_id"]
    assert first["run_ids"] == [RUN_ID, OTHER_RUN_ID]
    assert preflighted == [(Path("/opt/homebrew/bin/codex"), THREAD_ID)]
    paths = wakeup.wakeup_paths(args.state_root)
    subscriptions = wakeup.list_subscriptions(paths)
    assert len(subscriptions) == 1
    assert paths.pending_marker.is_file()
    assert os.stat(paths.root).st_mode & 0o777 == 0o700
    assert os.stat(paths.subscriptions_dir / f"{first['wake_id']}.json").st_mode & 0o777 == 0o600


def test_register_fails_before_persisting_when_controller_wakeup_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wakeup,
        "resolve_codex_executable",
        lambda _value: Path("/opt/homebrew/bin/codex"),
    )
    monkeypatch.setattr(
        wakeup,
        "call_controller",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unsupported")),
    )
    monkeypatch.setattr(
        wakeup,
        "preflight_thread",
        lambda *_args: pytest.fail("controller capability is checked first"),
    )
    args = register_args(tmp_path, config(tmp_path))

    with pytest.raises(RuntimeError, match="unsupported"):
        wakeup.register(args)

    assert wakeup.list_subscriptions(wakeup.wakeup_paths(args.state_root)) == []


def test_register_requires_a_codex_thread_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_registration(monkeypatch)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    args = register_args(tmp_path, config(tmp_path))
    args.codex_thread_id = None

    with pytest.raises(ValueError, match="thread id"):
        wakeup.register(args)


def test_register_relies_on_a_loaded_supervisor_instead_of_spawning_a_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_registration(monkeypatch)
    monkeypatch.setattr(
        wakeup,
        "_supervisor_status",
        lambda _paths: {"available": True, "installed": True, "loaded": True},
    )
    monkeypatch.setattr(
        wakeup,
        "start_worker",
        lambda _paths: pytest.fail("launchd owns worker startup"),
    )

    result = wakeup.register(register_args(tmp_path, config(tmp_path)))

    assert result["worker"] == {"started": False, "pid": None}
    assert result["supervisor"]["loaded"] is True


def test_cancel_archives_the_subscription_and_removes_pending_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_registration(monkeypatch)
    args = register_args(tmp_path, config(tmp_path))
    registered = wakeup.register(args)

    result = wakeup.cancel(
        argparse.Namespace(wake_id=registered["wake_id"], state_root=args.state_root)
    )

    paths = wakeup.wakeup_paths(args.state_root)
    assert result["status"] == "cancelled"
    assert wakeup.list_subscriptions(paths) == []
    assert not paths.pending_marker.exists()
    completed = wakeup._read_json(
        paths.completed_dir / f"{registered['wake_id']}.json"
    )
    assert completed["status"] == "cancelled"


def test_cohort_becomes_ready_only_when_every_run_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_registration(monkeypatch)
    args = register_args(tmp_path, config(tmp_path), run_ids=[RUN_ID, OTHER_RUN_ID])
    registered = wakeup.register(args)
    paths = wakeup.wakeup_paths(args.state_root)

    pending = wakeup.record_views(
        paths,
        registered["wake_id"],
        [
            run_view(RUN_ID, "terminal", outcome="succeeded"),
            run_view(OTHER_RUN_ID, "running", etag_character="b"),
        ],
    )
    ready = wakeup.record_views(
        paths,
        registered["wake_id"],
        [
            run_view(RUN_ID, "terminal", outcome="succeeded"),
            run_view(
                OTHER_RUN_ID,
                "terminal",
                outcome="failed",
                etag_character="c",
            ),
        ],
    )

    assert pending is not None and pending["status"] == "pending"
    assert ready is not None and ready["status"] == "ready"
    prompt = wakeup.build_wake_prompt(ready["ready_payload"])
    assert RUN_ID in prompt and OTHER_RUN_ID in prompt
    assert "outcome=succeeded" in prompt
    assert "outcome=failed" in prompt
    assert "must not reach" not in prompt
    assert "secret" not in prompt
    assert "Do not resubmit" in prompt


@pytest.mark.parametrize("phase", ["attention_required", "missing", "purged"])
def test_attention_or_unavailable_state_wakes_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    stub_registration(monkeypatch)
    args = register_args(tmp_path, config(tmp_path), run_ids=[RUN_ID, OTHER_RUN_ID])
    registered = wakeup.register(args)
    paths = wakeup.wakeup_paths(args.state_root)

    record = wakeup.record_views(
        paths,
        registered["wake_id"],
        [run_view(RUN_ID, phase), run_view(OTHER_RUN_ID, "running")],
    )

    assert record is not None and record["status"] == "ready"
    assert record["ready_payload"]["reason"] == "attention_required"


def test_poll_batch_uses_one_controller_call_for_the_cohort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_registration(monkeypatch)
    args = register_args(tmp_path, config(tmp_path), run_ids=[RUN_ID, OTHER_RUN_ID])
    wakeup.register(args)
    paths = wakeup.wakeup_paths(args.state_root)
    batch = wakeup.list_subscriptions(paths)
    observed: list[tuple[str, dict[str, Any], int]] = []

    def call(_config, action: str, **kwargs: Any) -> dict[str, Any]:
        observed.append((action, kwargs["payload"], kwargs["overall_timeout"]))
        return {
            "changed": True,
            "ready": False,
            "timed_out": False,
            "run_views": [
                run_view(RUN_ID, "running"),
                run_view(OTHER_RUN_ID, "running", etag_character="b"),
            ]
        }

    monkeypatch.setattr(wakeup, "call_controller", call)

    result = wakeup.poll_batch(paths, batch, wait_seconds=10)

    assert result["updated"] == 1
    assert observed[0][0] == "wait-runs"
    assert observed[0][1] == {
        "schema_version": 1,
        "wait_seconds": 10,
        "runs": [
            {"run_id": RUN_ID, "after_etag": "sha256:" + "a" * 64},
            {"run_id": OTHER_RUN_ID, "after_etag": "sha256:" + "b" * 64},
        ],
    }
    assert observed[0][2] == 28


def test_worker_delivers_ready_subscription_once_then_exits_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_registration(monkeypatch)
    args = register_args(tmp_path, config(tmp_path))
    registered = wakeup.register(args)
    paths = wakeup.wakeup_paths(args.state_root)
    ready = wakeup.record_views(
        paths,
        registered["wake_id"],
        [run_view(RUN_ID, "terminal", outcome="succeeded")],
    )
    assert ready is not None
    monkeypatch.setattr(
        wakeup_worker.time,
        "time",
        lambda: float(ready["delivery_not_before"]) + 1,
    )
    delivered: list[tuple[Path, str, str, str]] = []

    def deliver(
        executable: Path,
        thread_id: str,
        wake_id: str,
        prompt: str,
        *,
        start_if_missing: bool,
    ):
        delivered.append((executable, thread_id, wake_id, prompt))
        assert start_if_missing is True
        return {
            "wake_id": wake_id,
            "turn_id": "turn-1",
            "turn_status": "completed",
            "already_started": False,
        }

    monkeypatch.setattr(wakeup_worker, "deliver_wakeup", deliver)

    result = wakeup_worker.run_worker(paths)

    assert result == {"status": "idle", "processed": 1}
    assert len(delivered) == 1
    assert delivered[0][2] == registered["wake_id"]
    assert wakeup.list_subscriptions(paths) == []
    assert not paths.pending_marker.exists()
    completed = wakeup._read_json(
        paths.completed_dir / f"{registered['wake_id']}.json"
    )
    assert completed["status"] == "delivered"


def test_worker_persists_controller_failure_without_starting_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_registration(monkeypatch)
    args = register_args(tmp_path, config(tmp_path))
    registered = wakeup.register(args)
    paths = wakeup.wakeup_paths(args.state_root)
    monkeypatch.setattr(
        wakeup_worker,
        "poll_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(
        wakeup_worker,
        "deliver_wakeup",
        lambda *_args, **_kwargs: pytest.fail("Codex must not start before readiness"),
    )

    result = wakeup_worker.run_worker(paths, once=True)

    assert result["status"] == "controller_retryable"
    subscription = wakeup.list_subscriptions(paths)[0]
    assert subscription["wake_id"] == registered["wake_id"]
    assert subscription["controller_attempts"] == 1
    assert subscription["last_error"]["message"] == "offline"


def test_worker_inspects_history_before_retrying_an_ambiguous_turn_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_registration(monkeypatch)
    args = register_args(tmp_path, config(tmp_path))
    registered = wakeup.register(args)
    paths = wakeup.wakeup_paths(args.state_root)
    ready = wakeup.record_views(
        paths,
        registered["wake_id"],
        [run_view(RUN_ID, "terminal", outcome="succeeded")],
    )
    assert ready is not None
    now = [float(ready["delivery_not_before"]) + 1]
    monkeypatch.setattr(wakeup_worker.time, "time", lambda: now[0])
    starts: list[bool] = []

    def deliver(*_args: object, start_if_missing: bool, **_kwargs: object):
        starts.append(start_if_missing)
        if len(starts) < 3:
            raise codex_app_server.AppServerError("connection closed")
        return {
            "wake_id": registered["wake_id"],
            "turn_id": "turn-1",
            "turn_status": "completed",
            "already_started": False,
        }

    monkeypatch.setattr(wakeup_worker, "deliver_wakeup", deliver)

    first = wakeup_worker.run_worker(paths, once=True)
    now[0] += 1
    second = wakeup_worker.run_worker(paths, once=True)
    now[0] += wakeup.AMBIGUOUS_START_SECONDS
    third = wakeup_worker.run_worker(paths, once=True)

    assert first["status"] == "delivery_retryable"
    assert second["status"] == "delivery_retryable"
    assert third["status"] == "delivered"
    assert starts == [True, False, True]


def test_start_worker_detaches_the_internal_worker_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = wakeup.wakeup_paths(tmp_path / "state")
    observed: dict[str, Any] = {}

    class Process:
        pid = 456

    def popen(argv: list[str], **kwargs: Any) -> Process:
        observed.update(argv=argv, kwargs=kwargs)
        return Process()

    monkeypatch.setattr(wakeup, "_worker_active", lambda _paths: False)
    monkeypatch.setattr(wakeup.subprocess, "Popen", popen)

    result = wakeup.start_worker(paths)

    assert result == {"started": True, "pid": 456}
    assert observed["argv"][2:6] == [
        "remote_runner.cli",
        "wakeup",
        "worker",
        "--state-root",
    ]
    assert observed["kwargs"]["start_new_session"] is True
    assert observed["kwargs"]["stdin"] is subprocess.DEVNULL
