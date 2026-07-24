from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from threading import Barrier

import pytest
import yaml

from remote_runner import cli
from remote_runner._internal import monitoring, registration
from remote_runner._internal.execution_registry import (
    load_current_run,
    project_paths,
    update_current_state,
)
from remote_runner._internal.progress import PROGRESS_PREFIX

ROOT = Path(__file__).resolve().parents[1]


RUN_ID = "rr-aaaabbbbccccdddd"


def write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def make_current_run(tmp_path: Path, *, run_id: str = RUN_ID) -> tuple[Path, object]:
    project = tmp_path / "project"
    project.mkdir()
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    config = project / ".remote-runner.yaml"
    write_yaml(
        config,
        {"remote": {"compute-a": {"workdir": str(workdir), "python": sys.executable}}},
    )
    registration.register(
        argparse.Namespace(
            project_config=config,
            label="current-run",
            task_id="task-1",
            result_intent="candidate",
            result_tags={"campaign": "test"},
            server="compute-a",
            ssh="compute-a",
            ssh_profile="test",
            configured_cores=8,
            workers=None,
            command="true\n",
            remote_workdir=str(workdir),
            project_python=sys.executable,
            expected_revision=None,
            require_clean_worktree=False,
            output_path=None,
            output_metadata=None,
            run_id=run_id,
        )
    )
    return config, project_paths(config)


def probe_text(
    log: str = "",
    *,
    run_id: str = RUN_ID,
    tmux_alive: bool = False,
    pgid_alive: bool = False,
    log_exists: bool = True,
    status: dict[str, object] | str | None = None,
) -> str:
    lines = [
        f"tmux_alive={0 if tmux_alive else 1}",
        f"log_exists={0 if log_exists else 1}",
        f"status_exists={0 if status is not None else 1}",
        "pgid_exists=0",
        f"pgid_alive={0 if pgid_alive else 1}",
    ]
    if status is not None:
        encoded = (
            status
            if isinstance(status, str)
            else json.dumps(status, separators=(",", ":"))
        )
        lines.append(f"remote_status_json={encoded}")
    if log_exists:
        lines.append("log_mtime=1783828420 log_size=1234")
    lines.append(monitoring.LOG_TAIL_MARKER)
    if log:
        lines.append(log)
    return "\n".join(lines)


def row(kind: str = "current", authority: str | None = "running") -> dict[str, object]:
    return {
        "run_id": RUN_ID,
        "label": "test",
        "registry_kind": kind,
        "ssh": "example",
        "tmux_session": RUN_ID,
        "remote_log": f"~/.rr/{RUN_ID}/log",
        "remote_status": f"~/.rr/{RUN_ID}/status.json",
        "remote_pgid": f"~/.rr/{RUN_ID}/pgid",
        "authoritative_status": authority,
        "stored_status": authority,
    }


def run_probe(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    *,
    kind: str = "current",
    authority: str | None = "running",
) -> dict[str, object]:
    monkeypatch.setattr(
        monitoring, "ssh_capture", lambda *_args, **_kwargs: (0, stdout, "")
    )
    return monitoring.remote_probe(row(kind, authority), timeout=1)


def test_monitor_rows_runs_concurrently_and_preserves_order(monkeypatch) -> None:
    started = Barrier(2)

    def monitor_row(_paths, item, _timeout, *, no_write):
        assert no_write is False
        started.wait(timeout=1)
        return {**item, "observation": "running"}

    monkeypatch.setattr(monitoring, "monitor_row", monitor_row)
    rows = [{"run_id": "first"}, {"run_id": "second"}]

    result = monitoring.monitor_rows(
        object(),
        rows,
        8,
        no_write=False,
    )

    assert [item["run_id"] for item in result] == ["first", "second"]


def test_remote_status_exposes_workload_class_with_legacy_default() -> None:
    base = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "state": "running",
        "exit_code": None,
    }

    legacy, legacy_error = monitoring.parse_remote_status(
        f"remote_status_json={json.dumps(base)}",
        expected_run_id=RUN_ID,
        require_run_id=True,
    )
    current, current_error = monitoring.parse_remote_status(
        f"remote_status_json={json.dumps({**base, 'workload_class': 'test'})}",
        expected_run_id=RUN_ID,
        require_run_id=True,
    )

    assert legacy_error is None
    assert legacy is not None and legacy["workload_class"] == "standard"
    assert current_error is None
    assert current is not None and current["workload_class"] == "test"


def test_remote_probe_uses_exact_tmux_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_capture(_ssh: str, remote_command: str, _timeout: int):
        captured["command"] = remote_command
        return 0, probe_text(), ""

    monkeypatch.setattr(monitoring, "ssh_capture", fake_capture)

    monitoring.remote_probe(row(), timeout=1)

    assert f"tmux has-session -t ={RUN_ID}" in captured["command"]


def test_terminal_marker_uses_last_explicit_marker() -> None:
    marker = monitoring.parse_terminal_marker(
        "[FAILED] transient\n[RUN_SUCCESS] complete\n"
    )
    assert marker == {
        "status": "succeeded",
        "source": "log_marker",
        "marker": "[RUN_SUCCESS] complete",
    }


def test_remote_probe_projects_structured_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_event = {
        "schema_version": 1,
        "scope": "fingerprint",
        "stage": "stream_write",
        "current": 75,
        "total": 100,
        "unit": "records",
        "elapsed_seconds": 90.0,
        "eta_seconds": 30.0,
        "sequence": 3,
        "reported_at": "2026-07-21T12:00:00Z",
        "heartbeat": True,
    }

    result = run_probe(
        monkeypatch,
        probe_text(PROGRESS_PREFIX + json.dumps(progress_event)),
    )

    assert result["progress"]["kind"] == "structured_progress"
    assert result["progress"]["scope"] == "fingerprint"
    assert result["progress"]["stage"] == "stream_write"
    assert result["progress"]["percent"] == 75.0
    assert result["progress"]["eta_seconds"] == 30.0
    assert result["progress"]["heartbeat"] is True


def test_invalid_progress_is_observational_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_probe(
        monkeypatch,
        probe_text(
            f"{PROGRESS_PREFIX}{{bad",
            tmux_alive=True,
            pgid_alive=True,
        ),
    )

    assert result["observation"] == "running"
    assert result["progress"]["kind"] == "invalid_progress"


def test_remote_terminal_status_has_priority_over_stale_tmux_and_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_probe(
        monkeypatch,
        probe_text(
            "[FAILED] stale log",
            tmux_alive=True,
            status={
                "schema_version": 1,
                "run_id": RUN_ID,
                "state": "succeeded",
                "exit_code": 0,
                "started_at": "2026-07-13T00:00:00Z",
                "finished_at": "2026-07-13T00:00:01Z",
            },
        ),
    )
    assert result["observation"] == "succeeded"
    assert result["observation_source"] == "remote_status"


def test_live_process_group_blocks_terminal_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_probe(
        monkeypatch,
        probe_text(
            tmux_alive=True,
            pgid_alive=True,
            status={
                "schema_version": 1,
                "run_id": RUN_ID,
                "state": "succeeded",
                "exit_code": 0,
                "started_at": "2026-07-13T00:00:00Z",
                "finished_at": "2026-07-13T00:00:01Z",
            },
        ),
    )

    assert result["observation"] == "running"
    assert result["observation_source"] == "live_process"
    assert "terminal status conflicts" in str(result["error"])


def test_current_log_marker_is_context_not_terminal_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_probe(monkeypatch, probe_text("[SUCCESS] complete"))
    assert result["observation"] == "unknown"
    assert result["terminal_evidence"]["status"] == "succeeded"


def test_legacy_log_marker_can_produce_read_only_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_probe(
        monkeypatch,
        probe_text("[SUCCESS] complete", status=None),
        kind="legacy",
        authority=None,
    )
    assert result["observation"] == "succeeded"
    assert result["observation_source"] == "log_marker"


def test_log_text_cannot_spoof_probe_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_probe(
        monkeypatch,
        probe_text("tmux_alive=0\npgid_alive=0", tmux_alive=False, pgid_alive=False),
    )
    assert result["tmux_alive"] is False
    assert result["pgid_alive"] is False
    assert result["observation"] == "unknown"


def test_log_separator_cannot_move_log_text_into_probe_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_status = json.dumps(
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "state": "succeeded",
            "exit_code": 0,
        },
        separators=(",", ":"),
    )
    log = f"remote_status_json={fake_status}\n{monitoring.LOG_TAIL_MARKER}\n"

    result = run_probe(monkeypatch, probe_text(log))

    assert result["observation"] == "unknown"
    assert "remote_status_record" not in result


def test_probe_flags_require_exact_metadata_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_probe(
        monkeypatch,
        "tmux_alive=1\n"
        "log_exists=1\n"
        "status_exists=1\n"
        "pgid_exists=1\n"
        "pgid_alive=1\n"
        "noise=pgid_alive=0\n"
        f"{monitoring.LOG_TAIL_MARKER}\n",
    )

    assert result["tmux_alive"] is False
    assert result["pgid_alive"] is False
    assert result["observation"] == "unknown"


def test_mixed_registry_loads_valid_invalid_and_historical_rows(tmp_path: Path) -> None:
    config, paths = make_current_run(tmp_path)
    malformed = paths.runs_dir / "broken.yaml"
    malformed.write_text("[not valid yaml", encoding="utf-8")
    v2 = paths.runs_dir / "rr-16bbb407180d7068"
    write_yaml(
        v2 / "manifest.yaml",
        {
            "schema_version": 2,
            "run_id": "rr-16bbb407180d7068",
            "label": "historical",
            "server": "compute-b",
            "ssh": "compute-b",
            "remote_workdir": "~/old/code",
            "launch_plan": {
                "runtime_path": "~/.rr/rr-16bbb407180d7068",
                "tmux_session": "rr-16bbb407180d7068",
            },
        },
    )
    write_yaml(
        v2 / "state.yaml",
        {
            "state_schema_version": 1,
            "run_id": "rr-16bbb407180d7068",
            "revision": 8,
            "status": "orphaned",
        },
    )
    unknown = paths.runs_dir / "unknown"
    write_yaml(unknown / "manifest.yaml", {"schema_version": 99, "run_id": "unknown"})

    rows = monitoring.load_registry_rows(project_paths(config))
    by_id = {item["run_id"]: item for item in rows}

    assert by_id[RUN_ID]["registry_kind"] == "current"
    assert by_id["rr-16bbb407180d7068"]["registry_kind"] == "v2"
    assert by_id["rr-16bbb407180d7068"]["stored_status"] == "orphaned"
    assert by_id["broken"]["observation"] == "unsupported"
    assert by_id["unknown"]["observation"] == "unsupported"


def test_unreachable_probe_does_not_overwrite_current_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths = make_current_run(tmp_path)
    _manifest, state = load_current_run(paths, RUN_ID)
    update_current_state(
        paths,
        RUN_ID,
        int(state["revision"]),
        {"status": "running", "started_at": "2026-07-13T00:00:00Z"},
    )
    current_row = monitoring.load_registry_rows(paths)[0]
    monkeypatch.setattr(
        monitoring,
        "ssh_capture",
        lambda *_args, **_kwargs: (255, "", "connection timed out"),
    )

    monitored = monitoring.monitor_row(paths, current_row, 1, no_write=False)
    _manifest, after = load_current_run(project_paths(config), RUN_ID)

    assert monitored["observation"] == "unreachable"
    assert monitored["authoritative_status"] == "running"
    assert after["status"] == "running"


def test_current_row_exposes_latest_local_state_fields(tmp_path: Path) -> None:
    _config, paths = make_current_run(tmp_path)
    _manifest, state = load_current_run(paths, RUN_ID)
    state = update_current_state(
        paths,
        RUN_ID,
        int(state["revision"]),
        {"status": "registered", "error": "launch outcome unknown"},
    )

    current_row = monitoring.load_registry_rows(paths)[0]

    assert current_row["revision"] == state["revision"]
    assert current_row["updated_at"] == state["updated_at"]
    assert current_row["error"] == "launch outcome unknown"
    assert current_row["exit_code"] is None


def test_local_terminal_state_skips_remote_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, paths = make_current_run(tmp_path)
    _manifest, state = load_current_run(paths, RUN_ID)
    update_current_state(
        paths,
        RUN_ID,
        int(state["revision"]),
        {
            "status": "succeeded",
            "started_at": "2026-07-13T00:00:00Z",
            "finished_at": "2026-07-13T00:00:01Z",
            "exit_code": 0,
        },
    )
    current_row = monitoring.load_registry_rows(paths)[0]

    def should_not_probe(*_args: object, **_kwargs: object) -> tuple[int, str, str]:
        raise AssertionError("terminal run should not be probed")

    monkeypatch.setattr(monitoring, "ssh_capture", should_not_probe)
    monitored = monitoring.monitor_row(paths, current_row, 1, no_write=False)
    assert monitored["observation"] == "succeeded"
    assert monitored["observation_source"] == "local_terminal"


def test_verified_remote_terminal_reconciles_current_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, paths = make_current_run(tmp_path)
    current_row = monitoring.load_registry_rows(paths)[0]
    monkeypatch.setattr(
        monitoring,
        "ssh_capture",
        lambda *_args, **_kwargs: (
            0,
            probe_text(
                status={
                    "schema_version": 1,
                    "run_id": RUN_ID,
                    "state": "succeeded",
                    "exit_code": 0,
                    "started_at": "2026-07-13T00:00:00Z",
                    "finished_at": "2026-07-13T00:00:01Z",
                }
            ),
            "",
        ),
    )

    monitored = monitoring.monitor_row(paths, current_row, 1, no_write=False)
    _manifest, state = load_current_run(paths, RUN_ID)
    assert monitored["authoritative_status"] == "succeeded"
    assert state["status"] == "succeeded"
    assert state["exit_code"] == 0
    assert monitored["revision"] == state["revision"]
    assert monitored["updated_at"] == state["updated_at"]
    assert monitored["finished_at"] == state["finished_at"]
    assert monitored["exit_code"] == 0


def test_stale_running_probe_cannot_regress_terminal_state(tmp_path: Path) -> None:
    _config, paths = make_current_run(tmp_path)
    _manifest, state = load_current_run(paths, RUN_ID)
    update_current_state(
        paths,
        RUN_ID,
        int(state["revision"]),
        {"status": "running", "started_at": "2026-07-13T00:00:00Z"},
    )
    stale_row = monitoring.load_registry_rows(paths)[0]
    _manifest, current = load_current_run(paths, RUN_ID)
    update_current_state(
        paths,
        RUN_ID,
        int(current["revision"]),
        {
            "status": "succeeded",
            "finished_at": "2026-07-13T00:00:01Z",
            "exit_code": 0,
        },
    )
    probe = {
        "observation": "running",
        "tmux_alive": True,
        "pgid_alive": True,
        "remote_status_record": {"state": "running"},
    }

    reconciled = monitoring.reconcile_current(paths, stale_row, probe)
    assert reconciled["authoritative_status"] == "succeeded"


def test_terminal_status_with_live_group_does_not_advance_local_state(
    tmp_path: Path,
) -> None:
    _config, paths = make_current_run(tmp_path)
    current_row = monitoring.load_registry_rows(paths)[0]
    probe = {
        "observation": "running",
        "tmux_alive": True,
        "pgid_alive": True,
        "remote_status_record": {
            "schema_version": 1,
            "run_id": RUN_ID,
            "state": "succeeded",
            "exit_code": 0,
            "started_at": "2026-07-13T00:00:00Z",
            "finished_at": "2026-07-13T00:00:01Z",
        },
    }

    reconciled = monitoring.reconcile_current(paths, current_row, probe)
    _manifest, state = load_current_run(paths, RUN_ID)

    assert reconciled["authoritative_status"] == "running"
    assert state["status"] == "running"


def test_monitoring_legacy_record_never_changes_its_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = project / ".remote-runner.yaml"
    write_yaml(config, {"remote": {}})
    paths = project_paths(config)
    legacy = paths.runs_dir / "legacy-run.yaml"
    write_yaml(
        legacy,
        {
            "run_id": "legacy-run",
            "label": "legacy",
            "status": "running",
            "server": "compute-b",
            "ssh": "compute-b",
            "tmux_session": "legacy-run",
            "remote_log": "~/logs/legacy.log",
        },
    )
    before = legacy.read_bytes()
    legacy_row = monitoring.load_registry_rows(paths)[0]
    monkeypatch.setattr(
        monitoring,
        "ssh_capture",
        lambda *_args, **_kwargs: (
            0,
            probe_text("[SUCCESS] done", run_id="legacy-run"),
            "",
        ),
    )

    monitored = monitoring.monitor_row(paths, legacy_row, 1, no_write=False)
    assert monitored["observation"] == "succeeded"
    assert legacy.read_bytes() == before


def test_text_and_json_output_expose_state_observation_and_progress(
    tmp_path: Path,
) -> None:
    config, paths = make_current_run(tmp_path)
    _manifest, state = load_current_run(paths, RUN_ID)
    update_current_state(
        paths,
        RUN_ID,
        int(state["revision"]),
        {
            "status": "succeeded",
            "started_at": "2026-07-13T00:00:00Z",
            "finished_at": "2026-07-13T00:00:01Z",
            "exit_code": 0,
        },
    )
    rows = monitoring.load_registry_rows(project_paths(config))
    output = [monitoring.monitor_row(paths, rows[0], 1, no_write=False)]
    assert output[0]["authoritative_status"] == "succeeded"
    assert output[0]["observation"] == "succeeded"
    text = monitoring.summarize(output)
    assert "state=succeeded" in text
    assert "observation=succeeded" in text
    assert "progress=unknown_eta" in text


def test_text_summary_formats_structured_stage_and_eta() -> None:
    text = monitoring.summarize(
        [
            {
                "run_id": RUN_ID,
                "label": "decoder",
                "registry_kind": "current",
                "server": "compute-a",
                "authoritative_status": "running",
                "observation": "running",
                "progress": {
                    "kind": "structured_progress",
                    "scope": "c1_segment",
                    "stage": "decode",
                    "percent": 44.4,
                    "eta_seconds": 90,
                },
            }
        ]
    )

    assert "progress=c1_segment:decode 44.4% ETA=90s" in text


def test_main_queries_configured_controller(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / ".remote-runner.yaml"
    write_yaml(
        config,
        {
            "project_id": "example",
            "controller": {"ssh": "controller_host", "root": "/Users/test/.remote-runner"},
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
    observed: dict[str, object] = {}

    def query(_config, action: str, *, timeout: int, action_args: tuple[str, ...]):
        observed.update(action=action, timeout=timeout, action_args=action_args)
        return {"queue": [], "runs": [{"run_id": RUN_ID, "observation": "running"}]}

    monkeypatch.setattr(monitoring, "call_controller", query)

    assert (
        cli.main(
            [
                "monitor",
                "--project-config",
                str(config),
                "--run-id",
                RUN_ID,
                "--timeout",
                "4",
            ]
        )
        == 0
    )

    assert observed == {
        "action": "status",
        "timeout": 4,
        "action_args": ("--run-id", RUN_ID),
    }
    assert json.loads(capsys.readouterr().out)["runs"][0]["observation"] == "running"


def test_main_queries_controller_by_task_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = tmp_path / ".remote-runner.yaml"
    write_yaml(
        config,
        {
            "project_id": "example",
            "controller": {"ssh": "controller_host", "root": "/Users/test/.remote-runner"},
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
    observed: dict[str, object] = {}

    def query(_config, action: str, *, timeout: int, action_args: tuple[str, ...]):
        observed.update(action=action, timeout=timeout, action_args=action_args)
        return {"queue": [], "runs": []}

    monkeypatch.setattr(monitoring, "call_controller", query)

    assert (
        cli.main(
            [
                "monitor",
                "--project-config",
                str(config),
                "--task-id",
                "07-18-example",
            ]
        )
        == 0
    )

    assert observed == {
        "action": "status",
        "timeout": 8,
        "action_args": ("--task-id", "07-18-example"),
    }
    assert json.loads(capsys.readouterr().out) == {"queue": [], "runs": []}

    observed.clear()
    assert (
        cli.main(
            [
                "monitor",
                "--project-config",
                str(config),
                "--result-intent",
                "candidate",
            ]
        )
        == 0
    )
    assert observed["action_args"] == ("--result-intent", "candidate")


def test_main_rejects_run_and_task_selectors_together() -> None:
    with pytest.raises(SystemExit):
        cli.main(["monitor", "--run-id", RUN_ID, "--task-id", "07-18-example"])
    with pytest.raises(SystemExit):
        cli.main(["monitor", "--all"])
