from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from remote_runner import cli
from remote_runner._internal import launch, registration, stopping
from remote_runner._internal.execution_registry import (
    load_current_run,
    project_paths,
    update_current_state,
)
from remote_runner._internal.launch_plan import LaunchPlan, build_launch_plan

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.usefixtures("reap_test_runner_processes")


RUN_ID = "rr-fedcba9876543210"


def write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def register_local_run(
    tmp_path: Path,
    command: str,
    *,
    run_id: str = RUN_ID,
    label: str = "launch-test",
    expected_revision: str | None = None,
    project_python: str | None = None,
    output_root: str | None = None,
    output_relpath: str | None = None,
    output_path: str | None = None,
    workload_class: str = "standard",
) -> tuple[Path, LaunchPlan]:
    project = tmp_path / "project"
    workdir = tmp_path / "remote-workdir"
    project.mkdir()
    workdir.mkdir()
    config = project / ".remote-runner.yaml"
    write_yaml(
        config,
        {
            "remote": {
                "local": {
                    "workdir": str(workdir),
                    "python": project_python or sys.executable,
                }
            }
        },
    )
    args = argparse.Namespace(
        project_config=config,
        label=label,
        task_id="task-1",
        workload_class=workload_class,
        server="local",
        ssh="local",
        ssh_profile="test",
        configured_cores=8,
        workers=None,
        command=command,
        remote_workdir=str(workdir),
        project_python=project_python or sys.executable,
        expected_revision=expected_revision,
        require_clean_worktree=False,
        output_root=output_root,
        output_relpath=output_relpath,
        output_path=output_path,
        output_metadata=None,
        run_id=run_id,
    )
    registration.register(args)
    paths = project_paths(config)
    return config, build_launch_plan(paths, run_id)


def install_plan_runtime(plan: LaunchPlan, home: Path) -> Path:
    runtime = home / ".rr" / plan.run_id
    runtime.mkdir(parents=True, mode=0o700)
    os.chmod(runtime, 0o700)
    for asset in plan.assets:
        path = runtime / asset.name
        path.write_bytes(asset.content)
        path.chmod(asset.mode)
    log = runtime / "log"
    log.write_bytes(b"")
    log.chmod(0o600)
    return runtime


def wait_for_json(path: Path, timeout: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
            continue
        if isinstance(value, dict):
            return value
    raise AssertionError(f"timed out waiting for JSON: {path}")


@pytest.mark.parametrize(
    ("command", "expected_returncode", "expected_state"),
    [
        ("printf 'ok\\n'\n", 0, "succeeded"),
        ("printf 'bad\\n'\nexit 7\n", 7, "failed"),
        ("kill -TERM $$\n", 143, "failed"),
    ],
)
def test_normal_wrapper_preserves_terminal_result(
    tmp_path: Path,
    command: str,
    expected_returncode: int,
    expected_state: str,
) -> None:
    _config, plan = register_local_run(tmp_path, command)
    runtime = install_plan_runtime(plan, tmp_path / "home")

    completed = subprocess.run(
        ["bash", str(runtime / "run.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == expected_returncode, completed.stderr
    status_record = wait_for_json(runtime / "status.json")
    assert status_record["state"] == expected_state
    assert status_record["exit_code"] == expected_returncode
    assert status_record["started_at"]
    assert status_record["finished_at"]
    assert status_record["workload_class"] == "standard"
    assert status_record["label"] == "launch-test"
    log = (runtime / "log").read_text(encoding="utf-8")
    assert "[REMOTE_RUNNER_START]" in log
    assert f"state={expected_state} rc={expected_returncode}" in log


def test_wrapper_records_test_workload_class(tmp_path: Path) -> None:
    _config, plan = register_local_run(
        tmp_path,
        "printf 'ok\\n'\n",
        workload_class="test",
    )
    runtime = install_plan_runtime(plan, tmp_path / "home")

    subprocess.run(["bash", str(runtime / "run.sh")], check=True, timeout=10)

    status = wait_for_json(runtime / "status.json")
    assert status["workload_class"] == "test"
    assert status["label"] == "launch-test"


def test_wrapper_serializes_label_as_json(tmp_path: Path) -> None:
    label = 'hardware "compute-d" test'
    _config, plan = register_local_run(
        tmp_path,
        "printf 'ok\\n'\n",
        label=label,
    )
    runtime = install_plan_runtime(plan, tmp_path / "home")

    subprocess.run(["bash", str(runtime / "run.sh")], check=True, timeout=10)

    assert wait_for_json(runtime / "status.json")["label"] == label


def test_wrapper_waits_for_background_processes_in_workload_group(
    tmp_path: Path,
) -> None:
    started_path = tmp_path / "background.started"
    finished_path = tmp_path / "background.finished"
    command = f"""{sys.executable} - <<'PY' &
import pathlib
import time

pathlib.Path({str(started_path)!r}).write_text("started")
time.sleep(0.5)
pathlib.Path({str(finished_path)!r}).write_text("finished")
PY
"""
    _config, plan = register_local_run(tmp_path, command)
    runtime = install_plan_runtime(plan, tmp_path / "home")
    wrapper = subprocess.Popen(
        ["bash", str(runtime / "run.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not started_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started_path.exists()
        assert wrapper.poll() is None
        assert wait_for_json(runtime / "status.json")["state"] == "running"

        wrapper.wait(timeout=5)
        assert finished_path.is_file()
        assert wait_for_json(runtime / "status.json")["state"] == "succeeded"
    finally:
        if wrapper.poll() is None:
            wrapper.terminate()
            wrapper.wait(timeout=5)


def test_wrapper_keeps_runtime_files_after_changing_workdir(tmp_path: Path) -> None:
    config, plan = register_local_run(tmp_path, "pwd\n")
    runtime = install_plan_runtime(plan, tmp_path / "home")
    paths = project_paths(config)
    manifest, _state = load_current_run(paths, RUN_ID)

    subprocess.run(["bash", str(runtime / "run.sh")], check=True, timeout=10)

    assert (runtime / "status.json").is_file()
    assert (runtime / "log").is_file()
    assert str(manifest["remote_workdir"]) in (runtime / "log").read_text(encoding="utf-8")
    assert not (Path(str(manifest["remote_workdir"])) / "status.json").exists()


def test_launch_plan_is_normal_and_command_is_not_in_argv(tmp_path: Path) -> None:
    command = "python experiment.py --secret diagnostic-value\n"
    _config, plan = register_local_run(tmp_path, command)
    public = plan.public()
    argv_text = " ".join([*public["bootstrap_ssh_argv"], *public["tmux_argv"]])

    assert {asset.name for asset in plan.assets} == {"run.sh", "command.sh"}
    assert command.strip() not in argv_text
    assert "sitecustomize" not in plan.bootstrap_stdin.decode()
    assert "setproctitle" not in plan.bootstrap_stdin.decode()
    assert "privacy" not in next(
        asset.content.decode() for asset in plan.assets if asset.name == "run.sh"
    ).lower()


def test_launch_control_plane_uses_configured_project_python(tmp_path: Path) -> None:
    configured_python = "/opt/project env/bin/python"
    _config, plan = register_local_run(
        tmp_path,
        "true\n",
        project_python=configured_python,
    )
    wrapper = next(asset.content.decode() for asset in plan.assets if asset.name == "run.sh")

    assert plan.bootstrap_ssh_argv[-1] == f"{shlex.quote(configured_python)} -"
    assert f"{shlex.quote(configured_python)} -c " in wrapper
    assert "python3 -c " not in wrapper


def test_workload_receives_exact_configured_project_python(tmp_path: Path) -> None:
    configured_python = "/opt/project env/bin/python"
    _config, plan = register_local_run(
        tmp_path,
        "true\n",
        project_python=configured_python,
    )
    wrapper = next(
        asset.content.decode() for asset in plan.assets if asset.name == "run.sh"
    )
    quoted = shlex.quote(configured_python)

    assert f"RR_PROJECT_PYTHON={quoted} {quoted} -c " in wrapper
    assert "RR_PROJECT_PYTHON=$RR_PROJECT_PYTHON" not in wrapper


def test_workload_receives_exact_resolved_output_environment(tmp_path: Path) -> None:
    capture = tmp_path / "output-env.json"
    output_root = "/srv/project root;literal"
    output_relpath = "validation/run with spaces/result.json"
    output_path = f"{output_root}/{output_relpath}"
    command = f"""{sys.executable} - <<'PY'
import json
import os
from pathlib import Path

Path({str(capture)!r}).write_text(json.dumps({{
    "root": os.environ.get("RR_OUTPUT_ROOT"),
    "path": os.environ.get("RR_OUTPUT_PATH"),
    "dir": os.environ.get("RR_OUTPUT_DIR"),
}}))
PY
"""
    _config, plan = register_local_run(
        tmp_path,
        command,
        output_root=output_root,
        output_relpath=output_relpath,
        output_path=output_path,
    )
    runtime = install_plan_runtime(plan, tmp_path / "home")

    completed = subprocess.run(
        ["bash", str(runtime / "run.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "RR_OUTPUT_ROOT": "poison-root",
            "RR_OUTPUT_PATH": "poison-path",
            "RR_OUTPUT_DIR": "poison-dir",
        },
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert json.loads(capture.read_text(encoding="utf-8")) == {
        "root": output_root,
        "path": output_path,
        "dir": f"{output_root}/validation/run with spaces",
    }


def test_bootstrap_rejects_existing_resolved_output_before_runtime(
    tmp_path: Path,
) -> None:
    run_id = "rr-4444555566667777"
    output_root = tmp_path / "outputs"
    output_path = output_root / "validation" / "result.json"
    output_path.parent.mkdir(parents=True)
    output_path.write_text("existing", encoding="utf-8")
    _config, plan = register_local_run(
        tmp_path,
        "true\n",
        run_id=run_id,
        output_root=output_root.as_posix(),
        output_relpath="validation/result.json",
        output_path=output_path.as_posix(),
    )
    home = tmp_path / "home"
    home.mkdir()

    completed = subprocess.run(
        [sys.executable, "-"],
        input=plan.bootstrap_stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "HOME": str(home)},
        check=False,
        timeout=15,
    )

    result = launch._bootstrap_result(completed.stdout)
    assert completed.returncode == 1
    assert result is not None and result["ok"] is False
    assert "output path already exists" in str(result["message"])
    assert not (home / ".rr" / run_id).exists()


def test_wrapper_does_not_depend_on_path_python3(tmp_path: Path) -> None:
    _config, plan = register_local_run(tmp_path, "true\n")
    runtime = install_plan_runtime(plan, tmp_path / "home")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8")
    fake_python.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
    }

    completed = subprocess.run(
        ["bash", str(runtime / "run.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert wait_for_json(runtime / "status.json")["state"] == "succeeded"


def test_bootstrap_installs_private_runtime_and_starts_tmux(tmp_path: Path) -> None:
    run_id = "rr-1111222233334444"
    _config, plan = register_local_run(tmp_path, "sleep 0.2\nprintf 'done\\n'\n", run_id=run_id)
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)

    completed = subprocess.run(
        [sys.executable, "-"],
        input=plan.bootstrap_stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        timeout=15,
    )
    try:
        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        result = launch._bootstrap_result(completed.stdout)
        assert result is not None and result["ok"] is True
        assert result["tmux_started"] is True
        runtime = home / ".rr" / run_id
        assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
        assert stat.S_IMODE((runtime / "run.sh").stat().st_mode) == 0o700
        assert stat.S_IMODE((runtime / "command.sh").stat().st_mode) == 0o600
        status_record = wait_for_json(runtime / "status.json")
        deadline = time.monotonic() + 5
        while status_record["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            status_record = wait_for_json(runtime / "status.json")
        assert status_record["state"] == "succeeded"
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={run_id}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def test_bootstrap_preflight_failure_does_not_claim_runtime(tmp_path: Path) -> None:
    run_id = "rr-5555666677778888"
    _config, plan = register_local_run(
        tmp_path,
        "true\n",
        run_id=run_id,
        expected_revision="not-the-local-head",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)

    completed = subprocess.run(
        [sys.executable, "-"],
        input=plan.bootstrap_stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        timeout=15,
    )

    assert completed.returncode == 1
    result = launch._bootstrap_result(completed.stdout)
    assert result is not None and result["ok"] is False
    assert result["phase"] == "preflight"
    assert not (home / ".rr" / run_id).exists()


def test_launch_unknown_outcome_keeps_authoritative_state_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _plan = register_local_run(tmp_path, "sleep 1\n")
    paths = project_paths(config)

    def unknown(_plan: LaunchPlan, _timeout: int) -> dict[str, object]:
        raise launch.BootstrapOutcomeUnknown("connection dropped")

    monkeypatch.setattr(launch, "execute_plan", unknown)
    with pytest.raises(RuntimeError, match="connection dropped"):
        launch.launch(paths, RUN_ID, 1)

    _manifest, state = load_current_run(paths, RUN_ID)
    assert state["status"] == "registered"
    assert state["revision"] == 1
    assert state["error"] == "connection dropped"


def test_successful_launch_stays_running_until_monitor_confirms_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _plan = register_local_run(tmp_path, "true\n")
    paths = project_paths(config)
    monkeypatch.setattr(
        launch,
        "execute_plan",
        lambda *_args: {
            "ok": True,
            "tmux_started": True,
            "status": {
                "state": "succeeded",
                "started_at": "2026-07-13T00:00:00Z",
                "finished_at": "2026-07-13T00:00:01Z",
                "exit_code": 0,
            },
        },
    )

    state, _plan = launch.launch(paths, RUN_ID, 1)

    assert state["status"] == "running"
    assert state["finished_at"] is None
    assert state["exit_code"] is None


def test_transport_timeouts_cover_connect_and_remote_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, plan = register_local_run(tmp_path, "true\n")
    observed: list[float] = []

    def completed(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        observed.append(float(kwargs["timeout"]))
        stdin = kwargs["input"]
        assert isinstance(stdin, bytes)
        prefix = "RR_STOP_RESULT" if b"RR_STOP_RESULT" in stdin else "RR_BOOTSTRAP_RESULT"
        payload = (
            {"ok": True, "tmux_started": True, "status": {"state": "running"}}
            if prefix == "RR_BOOTSTRAP_RESULT"
            else {"ok": True, "action": "stopped", "status": {"state": "stopped"}}
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{prefix} {json.dumps(payload)}\n".encode(),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", completed)
    launch.execute_plan(plan, 12)
    stopping.execute_stop("example", sys.executable, RUN_ID, 12)

    assert observed == [87.0, 39.0]


@pytest.mark.parametrize(
    "operation",
    [
        lambda plan: launch.execute_plan(plan, 0),
        lambda plan: stopping.execute_stop("example", sys.executable, plan.run_id, 0),
    ],
)
def test_transport_timeouts_must_be_positive(
    tmp_path: Path,
    operation: Callable[[LaunchPlan], object],
) -> None:
    _config, plan = register_local_run(tmp_path, "true\n")
    with pytest.raises(ValueError, match="timeout must be positive"):
        operation(plan)


def test_stop_control_plane_uses_configured_project_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_python = "/opt/project env/bin/python"
    observed: list[list[str]] = []

    def completed(
        argv: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        observed.append(argv)
        result = {
            "ok": True,
            "action": "stopped",
            "status": {"state": "stopped"},
        }
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"RR_STOP_RESULT {json.dumps(result)}\n".encode(),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", completed)
    stopping.execute_stop("example", configured_python, RUN_ID, 12)

    assert observed[0][-1] == f"{shlex.quote(configured_python)} -"


def test_stop_main_queries_configured_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / ".remote-runner.yaml"
    write_yaml(
        config,
        {
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

    def stop_controller(
        _config,
        action: str,
        *,
        timeout: int,
        action_args: tuple[str, ...],
    ) -> dict[str, object]:
        observed.update(action=action, timeout=timeout, action_args=action_args)
        return {"kind": "queue", "state": {"status": "stopped"}}

    monkeypatch.setattr(stopping, "call_controller", stop_controller)

    assert cli.main(
        [
            "stop",
            "--project-config",
            str(config),
            "--run-id",
            RUN_ID,
            "--timeout",
            "4",
        ]
    ) == 0

    assert observed == {
        "action": "stop",
        "timeout": 4,
        "action_args": ("--run-id", RUN_ID),
    }
    assert json.loads(capsys.readouterr().out)["state"]["status"] == "stopped"


def test_legacy_manifest_cannot_be_relaunched(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = project / ".remote-runner.yaml"
    write_yaml(config, {"remote": {}})
    paths = project_paths(config)
    paths.runs_dir.mkdir(parents=True)
    write_yaml(paths.runs_dir / f"{RUN_ID}.yaml", {"run_id": RUN_ID})

    with pytest.raises(ValueError, match="only current-format"):
        launch.launch(paths, RUN_ID, 1)


def test_stop_program_terminates_complete_process_group(tmp_path: Path) -> None:
    home = tmp_path / "home"
    worker_pid_path = tmp_path / "worker.pid"
    child_pid_path = tmp_path / "child.pid"
    command = f"""{sys.executable} - <<'PY'
import pathlib
import subprocess
import os

pathlib.Path({str(worker_pid_path)!r}).write_text(str(os.getpid()))
child = subprocess.Popen([
    {sys.executable!r},
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
])
pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))
child.wait()
PY
"""
    _config, plan = register_local_run(tmp_path, command)
    runtime = install_plan_runtime(plan, home)
    wrapper = subprocess.Popen(
        ["bash", str(runtime / "run.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (runtime / "pgid").exists() and child_pid_path.exists():
                break
            time.sleep(0.02)
        else:
            raise AssertionError("workload process group did not start")
        pgid = int((runtime / "pgid").read_text(encoding="utf-8"))
        assert pgid > 1

        env = os.environ.copy()
        env["HOME"] = str(home)
        stopped = subprocess.run(
            [sys.executable, "-"],
            input=stopping.build_stop_stdin(RUN_ID, 1.0),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
            timeout=15,
        )
        assert stopped.returncode == 0, stopped.stderr.decode(errors="replace")
        wrapper.wait(timeout=10)
        status_record = wait_for_json(runtime / "status.json")
        assert status_record["state"] == "stopped"
        assert (runtime / "stop.request").is_file()

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("workload process group survived stop")
    finally:
        if wrapper.poll() is None:
            wrapper.terminate()
            wrapper.wait(timeout=5)


def test_stop_does_not_trust_terminal_status_while_group_is_alive(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _config, plan = register_local_run(tmp_path, "sleep 60\n")
    runtime = install_plan_runtime(plan, home)
    wrapper = subprocess.Popen(
        ["bash", str(runtime / "run.sh")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not (runtime / "pgid").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (runtime / "pgid").exists()
        status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
        status["state"] = "succeeded"
        status["exit_code"] = 0
        (runtime / "status.json").write_text(json.dumps(status), encoding="utf-8")

        env = os.environ.copy()
        env["HOME"] = str(home)
        stopped = subprocess.run(
            [sys.executable, "-"],
            input=stopping.build_stop_stdin(RUN_ID, 0.2),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
            timeout=15,
        )

        assert stopped.returncode == 0, stopped.stderr.decode(errors="replace")
        result = stopping._stop_result(stopped.stdout)
        assert result is not None and result["action"] == "stopped"
        wrapper.wait(timeout=10)
    finally:
        if wrapper.poll() is None:
            wrapper.terminate()
            wrapper.wait(timeout=5)


def test_stop_cleans_tmux_before_returning_for_finished_group(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _config, plan = register_local_run(tmp_path, "true\n")
    runtime = install_plan_runtime(plan, home)
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "state": "succeeded",
                "exit_code": 0,
            }
        ),
        encoding="utf-8",
    )
    (runtime / "pgid").write_text("999999\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "tmux.calls"
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {str(call_log)!r}\nexit 1\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
    }
    completed = subprocess.run(
        [sys.executable, "-"],
        input=stopping.build_stop_stdin(RUN_ID, 1.0),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        f"kill-session -t ={RUN_ID}",
        f"has-session -t ={RUN_ID}",
    ]


def test_stop_refuses_unowned_process_group(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = home / ".rr" / RUN_ID
    runtime.mkdir(parents=True)
    (runtime / "status.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "state": "running",
                "exit_code": None,
            }
        ),
        encoding="utf-8",
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        (runtime / "pgid").write_text(f"{worker.pid}\n", encoding="utf-8")
        (runtime / "owner.json").write_text(
            json.dumps({"run_id": RUN_ID, "pid": worker.pid + 1, "pgid": worker.pid + 1}),
            encoding="utf-8",
        )

        completed = subprocess.run(
            [sys.executable, "-"],
            input=stopping.build_stop_stdin(RUN_ID, 1.0),
            env={**os.environ, "HOME": str(home)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )

        assert completed.returncode == 2
        result = stopping._stop_result(completed.stdout)
        assert result is not None
        assert result["action"] == "unknown"
        assert "does not match" in result["message"]
        assert worker.poll() is None
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=5)


def test_stop_updates_local_state_to_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _plan = register_local_run(tmp_path, "sleep 60\n")
    paths = project_paths(config)
    _manifest, state = load_current_run(paths, RUN_ID)
    state = update_current_state(
        paths,
        RUN_ID,
        int(state["revision"]),
        {"status": "running", "started_at": "2026-07-13T00:00:00Z"},
    )

    monkeypatch.setattr(
        stopping,
        "execute_stop",
        lambda *_args: {
            "ok": True,
            "action": "stopped",
            "status": {
                "state": "stopped",
                "started_at": "2026-07-13T00:00:00Z",
                "finished_at": "2026-07-13T00:00:01Z",
                "exit_code": 143,
            },
        },
    )
    stopped = stopping.stop(paths, RUN_ID, 1)

    assert stopped["status"] == "stopped"
    assert stopped["exit_code"] == 143
    assert stopped["revision"] == state["revision"] + 1


def test_stop_unknown_does_not_claim_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _plan = register_local_run(tmp_path, "sleep 60\n")
    paths = project_paths(config)
    _manifest, state = load_current_run(paths, RUN_ID)
    update_current_state(
        paths,
        RUN_ID,
        int(state["revision"]),
        {"status": "running", "started_at": "2026-07-13T00:00:00Z"},
    )

    def unknown(*_args: object) -> dict[str, object]:
        raise stopping.StopOutcomeUnknown("ssh disconnected")

    monkeypatch.setattr(stopping, "execute_stop", unknown)
    with pytest.raises(RuntimeError, match="ssh disconnected"):
        stopping.stop(paths, RUN_ID, 1)

    _manifest, current = load_current_run(paths, RUN_ID)
    assert current["status"] == "running"
    assert current["error"] == "ssh disconnected"


def test_runtime_assets_have_expected_modes(tmp_path: Path) -> None:
    _config, plan = register_local_run(tmp_path, "true\n")
    runtime = install_plan_runtime(plan, tmp_path / "home")

    assert stat.S_IMODE((runtime / "run.sh").stat().st_mode) == 0o700
    assert stat.S_IMODE((runtime / "command.sh").stat().st_mode) == 0o600
    assert stat.S_IMODE((runtime / "log").stat().st_mode) == 0o600
