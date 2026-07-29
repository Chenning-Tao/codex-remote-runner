from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from remote_runner import cli
from remote_runner._internal import waiting
from remote_runner._internal.execution_registry import write_yaml


RUN_ID = "rr-0123456789abcdef"


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


def view(
    phase: str,
    *,
    outcome: str | None = None,
    etag_character: str = "a",
    output_sync_status: str = "not_enqueued",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "example",
        "run_id": RUN_ID,
        "etag": "sha256:" + etag_character * 64,
        "phase": phase,
        "outcome": outcome,
        "terminal_source": "execution" if outcome is not None else None,
        "queue": None,
        "execution": None,
        "output_sync": {"status": output_sync_status},
        "purge": None,
    }


def args(config_path: Path, **changes: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "project_config": config_path,
        "run_id": RUN_ID,
        "timeout": 8,
        "until": "execution-terminal",
        "max_wait": None,
        "connection_grace": None,
    }
    values.update(changes)
    return argparse.Namespace(**values)


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "stopped"])
def test_wait_treats_every_authoritative_terminal_outcome_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    calls: list[str] = []

    def call(_config, action: str, **_kwargs):
        calls.append(action)
        return {"run_view": view("terminal", outcome=outcome)}

    monkeypatch.setattr(waiting, "call_controller", call)
    result = waiting.wait_for_run(args(config(tmp_path)), reporter=lambda _line: None)

    assert result["wait_status"] == "completed"
    assert result["run_view"]["outcome"] == outcome
    assert waiting.wait_exit_code(result) == 0
    assert calls == ["status"]


def test_wait_uses_etag_long_poll_until_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, tuple[str, ...], int | None]] = []
    responses = iter(
        [
            {"run_view": view("queued")},
            {
                "changed": True,
                "timed_out": False,
                "run_view": view(
                    "terminal",
                    outcome="succeeded",
                    etag_character="b",
                ),
            },
        ]
    )

    def call(
        _config,
        action: str,
        *,
        timeout: int,
        action_args: tuple[str, ...],
        overall_timeout: int | None,
    ):
        assert timeout == 8
        observed.append((action, action_args, overall_timeout))
        return next(responses)

    monkeypatch.setattr(waiting, "call_controller", call)
    result = waiting.wait_for_run(args(config(tmp_path)), reporter=lambda _line: None)

    assert result["wait_status"] == "completed"
    assert result["controller_calls"] == 2
    assert observed[0] == ("status", ("--run-id", RUN_ID), None)
    assert observed[1][0] == "wait-run"
    assert observed[1][1] == (
        "--run-id",
        RUN_ID,
        "--after-etag",
        "sha256:" + "a" * 64,
        "--wait-seconds",
        "50",
    )
    assert observed[1][2] == 68


def test_reportable_wait_continues_until_output_sync_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    responses = iter(
        [
            {
                "run_view": view(
                    "terminal",
                    outcome="succeeded",
                    output_sync_status="pending",
                )
            },
            {
                "changed": True,
                "timed_out": False,
                "run_view": view(
                    "terminal",
                    outcome="succeeded",
                    etag_character="b",
                    output_sync_status="completed",
                ),
            },
        ]
    )

    def call(_config, action: str, **_kwargs):
        calls.append(action)
        return next(responses)

    monkeypatch.setattr(waiting, "call_controller", call)
    result = waiting.wait_for_run(
        args(config(tmp_path), until="reportable"),
        reporter=lambda _line: None,
    )

    assert result["wait_status"] == "completed"
    assert result["run_view"]["output_sync"]["status"] == "completed"
    assert calls == ["status", "wait-run"]


def test_reportable_wait_throttles_an_older_terminal_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            {
                "run_view": view(
                    "terminal",
                    outcome="succeeded",
                    output_sync_status="pending",
                )
            },
            {
                "changed": False,
                "timed_out": False,
                "run_view": view(
                    "terminal",
                    outcome="succeeded",
                    output_sync_status="pending",
                ),
            },
            {
                "changed": True,
                "timed_out": False,
                "run_view": view(
                    "terminal",
                    outcome="succeeded",
                    etag_character="b",
                    output_sync_status="completed",
                ),
            },
        ]
    )
    delays: list[float] = []
    monkeypatch.setattr(waiting, "call_controller", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(waiting.time, "sleep", delays.append)

    result = waiting.wait_for_run(
        args(config(tmp_path), until="reportable"),
        reporter=lambda _line: None,
    )

    assert result["wait_status"] == "completed"
    assert delays == [waiting.LEGACY_TERMINAL_BACKOFF_SECONDS]


def test_reportable_wait_does_not_delay_a_failed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def call(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "run_view": view(
                "terminal",
                outcome="failed",
                output_sync_status="pending",
            )
        }

    monkeypatch.setattr(waiting, "call_controller", call)
    result = waiting.wait_for_run(
        args(config(tmp_path), until="reportable"),
        reporter=lambda _line: None,
    )

    assert result["wait_status"] == "completed"
    assert calls == 1


def test_reportable_wait_surfaces_cancelled_output_sync_as_attention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        waiting,
        "call_controller",
        lambda *_args, **_kwargs: {
            "run_view": view(
                "terminal",
                outcome="succeeded",
                output_sync_status="cancelled",
            )
        },
    )

    result = waiting.wait_for_run(
        args(config(tmp_path), until="reportable"),
        reporter=lambda _line: None,
    )

    assert result["wait_status"] == "attention_required"
    assert waiting.wait_exit_code(result) == 4


def test_wait_retries_a_transient_controller_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    reports: list[str] = []

    def call(_config, _action: str, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary disconnect")
        return {"run_view": view("terminal", outcome="failed")}

    monkeypatch.setattr(waiting, "call_controller", call)
    monkeypatch.setattr(waiting.time, "sleep", lambda _seconds: None)
    result = waiting.wait_for_run(args(config(tmp_path)), reporter=reports.append)

    assert result["wait_status"] == "completed"
    assert result["transport_retries"] == 1
    assert attempts == 2
    assert "retrying in 1s" in reports[0]


def test_wait_retries_controller_failures_indefinitely_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    attempts = 0

    def call(_config, _action: str, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first disconnect")
        if attempts == 2:
            now[0] = 301.0
            raise RuntimeError("still disconnected")
        return {"run_view": view("terminal", outcome="succeeded")}

    monkeypatch.setattr(waiting.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(waiting.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(waiting, "call_controller", call)

    result = waiting.wait_for_run(args(config(tmp_path)), reporter=lambda _line: None)

    assert result["wait_status"] == "completed"
    assert result["transport_retries"] == 2
    assert attempts == 3


def test_explicit_connection_grace_ends_an_unreachable_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    attempts = 0

    def call(_config, _action: str, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            now[0] = 301.0
        raise RuntimeError("controller unavailable")

    monkeypatch.setattr(waiting.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(waiting.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(waiting, "call_controller", call)

    with pytest.raises(RuntimeError, match="beyond --connection-grace"):
        waiting.wait_for_run(
            args(config(tmp_path), connection_grace=300),
            reporter=lambda _line: None,
        )

    assert attempts == 2


def test_wait_deadline_returns_last_view_without_stopping_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moments = iter((0.0, 0.0, 1.1, 1.1))
    monkeypatch.setattr(waiting.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(
        waiting,
        "call_controller",
        lambda *_args, **_kwargs: {"run_view": view("running")},
    )

    result = waiting.wait_for_run(
        args(config(tmp_path), max_wait=1),
        reporter=lambda _line: None,
    )

    assert result["wait_status"] == "timed_out"
    assert result["run_view"]["phase"] == "running"
    assert waiting.wait_exit_code(result) == 3


@pytest.mark.parametrize(
    ("phase", "exit_code"),
    [("attention_required", 4), ("missing", 4), ("purged", 4)],
)
def test_wait_stops_on_attention_or_an_unavailable_run_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    exit_code: int,
) -> None:
    monkeypatch.setattr(
        waiting,
        "call_controller",
        lambda *_args, **_kwargs: {"run_view": view(phase)},
    )

    result = waiting.wait_for_run(args(config(tmp_path)), reporter=lambda _line: None)

    assert result["wait_status"] == phase
    assert waiting.wait_exit_code(result) == exit_code


def test_run_wait_combines_submission_and_wait_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli.submission,
        "submit",
        lambda _args: {"run_id": RUN_ID, "revision": "a" * 40},
    )
    completed = {
        "wait_status": "completed",
        "run_view": view("terminal", outcome="failed"),
    }
    monkeypatch.setattr(cli.waiting, "wait_for_run", lambda _args: completed)
    result, returncode = cli._execute(
        argparse.Namespace(
            subcommand="run",
            wait=True,
            max_wait=None,
            connection_grace=None,
            until="execution-terminal",
            project_config=Path("/tmp/project.yaml"),
            timeout=8,
        )
    )

    assert result["run_id"] == RUN_ID
    assert result["wait"] == completed
    assert returncode == 0
    assert f"submitted run_id={RUN_ID}; waiting" in capsys.readouterr().err


def test_wait_cli_writes_one_final_json_result_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    completed = {
        "wait_status": "completed",
        "run_view": view(
            "terminal",
            outcome="succeeded",
            output_sync_status="completed",
        ),
    }
    monkeypatch.setattr(cli.waiting, "wait_for_run", lambda _args: completed)

    returncode = cli.main(
        [
            "wait",
            "--run-id",
            RUN_ID,
            "--until",
            "reportable",
        ]
    )

    captured = capsys.readouterr()
    assert returncode == 0
    assert json.loads(captured.out) == completed
    assert captured.err == ""


def test_run_rejects_max_wait_before_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli.submission,
        "submit",
        lambda _args: pytest.fail("invalid wait options must not submit a run"),
    )

    with pytest.raises(ValueError, match="--max-wait requires --wait"):
        cli._execute(argparse.Namespace(subcommand="run", wait=False, max_wait=60))


def test_run_rejects_connection_grace_before_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli.submission,
        "submit",
        lambda _args: pytest.fail("invalid wait options must not submit a run"),
    )

    with pytest.raises(ValueError, match="--connection-grace requires --wait"):
        cli._execute(
            argparse.Namespace(
                subcommand="run",
                wait=False,
                max_wait=None,
                connection_grace=60,
            )
        )
