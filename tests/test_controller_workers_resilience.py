from __future__ import annotations

from pathlib import Path

import pytest

from remote_runner._internal.controller import dispatcher, output_sync_worker
from remote_runner._internal.controller.dispatcher import DispatchOutcome
from remote_runner._internal.controller.registry import controller_paths
from remote_runner._internal.execution_registry import write_yaml


def test_dispatch_loop_survives_cycle_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    calls = 0

    def flaky_batch(_paths, *, timeout=8):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated transient dispatch failure")
        return [DispatchOutcome(action="idle", run_id=None)]

    monkeypatch.setattr(dispatcher, "dispatch_batch", flaky_batch)

    assert dispatcher.dispatch_loop(paths, timeout=8, interval_seconds=0.01) == 0
    assert calls == 2
    assert "cycle failed" in capsys.readouterr().err


def test_dispatch_loop_monitors_with_isolated_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    paths.project_root.mkdir(parents=True)
    write_yaml(paths.config_path, {"controller_registry": True})
    seen: dict[str, bool] = {}
    monkeypatch.setattr(
        dispatcher.monitoring, "load_registry_rows", lambda _paths: []
    )

    def fake_monitor_rows(
        _paths, rows, timeout, *, no_write, isolate_errors
    ):
        seen["isolate_errors"] = isolate_errors
        assert rows == []
        return []

    monkeypatch.setattr(dispatcher.monitoring, "monitor_rows", fake_monitor_rows)
    monkeypatch.setattr(
        dispatcher,
        "dispatch_batch",
        lambda _paths, **kwargs: [DispatchOutcome(action="idle", run_id=None)],
    )

    assert dispatcher.dispatch_loop(paths, timeout=8, interval_seconds=0.01) == 0
    assert seen["isolate_errors"] is True


def test_dispatch_loop_reaches_queue_after_multiple_terminal_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    paths.project_root.mkdir(parents=True)
    write_yaml(paths.config_path, {"controller_registry": True})
    terminal_rows = [
        {
            "run_id": "rr-1111111111111111",
            "registry_kind": "current",
            "authoritative_status": "succeeded",
        },
        {
            "run_id": "rr-2222222222222222",
            "registry_kind": "current",
            "authoritative_status": "failed",
        },
    ]
    monkeypatch.setattr(
        dispatcher.monitoring, "load_registry_rows", lambda _paths: terminal_rows
    )
    monkeypatch.setattr(
        dispatcher.monitoring,
        "remote_probe",
        lambda *_args, **_kwargs: pytest.fail("terminal runs must not be probed"),
    )
    monkeypatch.setattr(
        dispatcher.monitoring,
        "remote_probe_many",
        lambda *_args, **_kwargs: pytest.fail("terminal runs must not be probed"),
    )
    batches = iter(
        (
            [DispatchOutcome(action="started", run_id="rr-3333333333333333")],
            [DispatchOutcome(action="idle", run_id=None)],
            [DispatchOutcome(action="idle", run_id=None)],
        )
    )
    actions: list[str] = []

    def dispatch_batch(_paths, *, timeout=8):
        outcomes = next(batches)
        actions.extend(outcome.action for outcome in outcomes)
        return outcomes

    sleeps = 0

    def bounded_sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            pytest.fail("dispatcher never reached the queued job")

    monkeypatch.setattr(dispatcher, "dispatch_batch", dispatch_batch)
    monkeypatch.setattr(
        dispatcher, "ensure_output_sync_worker", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(dispatcher.time, "sleep", bounded_sleep)

    assert dispatcher.dispatch_loop(paths, timeout=8, interval_seconds=0.01) == 0
    assert actions == ["started", "idle", "idle"]
    assert sleeps == 1
    assert "cycle failed" not in capsys.readouterr().err


def test_output_sync_worker_survives_cycle_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    paths.project_root.mkdir(parents=True)
    write_yaml(paths.config_path, {"controller_registry": True})
    calls = 0

    def flaky_process(_execution_paths, *, connect_timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("simulated corrupted intent")
        return {
            "enabled": False,
            "pending": 0,
            "processed": 0,
            "remaining": 0,
        }

    monkeypatch.setattr(output_sync_worker, "process_pending_once", flaky_process)

    assert (
        output_sync_worker.run_worker(paths, timeout=8, interval=0.01, once=False)
        == 0
    )
    assert calls == 2
