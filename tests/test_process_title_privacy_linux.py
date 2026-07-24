from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import yaml

from remote_runner._internal import launch, launch_plan, registration, stopping
from remote_runner._internal.execution_registry import PROCESS_TITLE_PRIVACY_MODE, project_paths


pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="process-title effects require Linux /proc and procps",
)


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _register_plan(
    config: Path,
    *,
    run_id: str,
    command: str,
    privacy: str | None,
) -> launch_plan.LaunchPlan:
    runtime = yaml.safe_load(config.read_text(encoding="utf-8"))["remote"]["local"]
    registration.register(
        argparse.Namespace(
            project_config=config,
            label="Linux process-title effect test",
            task_id="07-12-remote-process-privacy",
            server="local",
            ssh="local",
            ssh_profile="test",
            configured_cores=2,
            workers=None,
            command=command,
            remote_workdir=runtime["workdir"],
            project_python=runtime["python"],
            expected_revision=None,
            require_clean_worktree=False,
            output_path=None,
            output_metadata=None,
            run_id=run_id,
            privacy=privacy,
        )
    )
    return launch_plan.build_launch_plan(project_paths(config), run_id)


def _launch_locally(plan: launch_plan.LaunchPlan, home: Path) -> Path:
    env = os.environ.copy()
    env["HOME"] = str(home)
    completed = subprocess.run(
        shlex.split(plan.bootstrap_ssh_argv[-1]),
        input=plan.bootstrap_stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        timeout=30,
    )
    result = launch._bootstrap_result(completed.stdout)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert result is not None and result["ok"] is True
    assert result["tmux_started"] is True
    return home / ".rr" / plan.run_id


def _wait_for_pid(marker: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(marker.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.05)
    raise AssertionError(f"timed out waiting for workload marker: {marker}")


def _process_arguments(pid: int) -> tuple[bytes, str]:
    proc_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    listed = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "args="],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert listed.returncode == 0, listed.stderr
    return proc_cmdline, listed.stdout.strip()


def _read_pgid(runtime: Path) -> int:
    return int((runtime / "pgid").read_text(encoding="utf-8").strip())


def _stop_locally(home: Path, run_id: str) -> dict[str, object]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    completed = subprocess.run(
        [sys.executable, "-"],
        input=stopping.build_stop_stdin(run_id, 3.0),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
        timeout=20,
    )
    result = stopping._stop_result(completed.stdout)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert result is not None and result["ok"] is True
    return result


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _force_cleanup(home: Path, run_id: str) -> None:
    runtime = home / ".rr" / run_id
    if runtime.is_dir():
        env = {**os.environ, "HOME": str(home)}
        try:
            subprocess.run(
                [sys.executable, "-"],
                input=stopping.build_stop_stdin(run_id, 1.0),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                check=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            pass
    subprocess.run(
        ["tmux", "kill-session", "-t", f"={run_id}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    try:
        owner = json.loads((runtime / "owner.json").read_text(encoding="utf-8"))
        if not isinstance(owner, dict):
            return
        pgid = int(owner["pgid"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return
    if (
        owner.get("run_id") == run_id
        and owner.get("pid") == pgid
        and _group_exists(pgid)
    ):
        os.killpg(pgid, signal.SIGKILL)


def test_opt_in_masks_stable_python_argv_and_normal_mode_does_not(
    tmp_path: Path,
) -> None:
    assert shutil.which("tmux") is not None, "Linux integration requires tmux"
    assert shutil.which("ps") is not None, "Linux integration requires procps"
    dependency = subprocess.run(
        [sys.executable, "-c", "import setproctitle"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert dependency.returncode == 0, (
        "Linux integration requires setproctitle in the test Python: "
        + dependency.stderr
    )

    project = tmp_path / "project"
    workdir = tmp_path / "workdir"
    home = tmp_path / "home"
    project.mkdir()
    workdir.mkdir()
    home.mkdir()
    config = project / ".remote-runner.yaml"
    _write_yaml(
        config,
        {
            "remote": {
                "local": {
                    "workdir": str(workdir),
                    "python": sys.executable,
                }
            }
        },
    )

    normal_run_id = "rr-" + uuid.uuid4().hex[:16]
    private_run_id = "rr-" + uuid.uuid4().hex[:16]
    normal_canary = "normal-argv-canary-" + uuid.uuid4().hex
    private_canary = "private-argv-canary-" + uuid.uuid4().hex
    normal_marker = tmp_path / "normal.pid"
    private_marker = tmp_path / "private.pid"

    def command(marker: Path, canary: str) -> str:
        source = (
            "import os, pathlib, time; "
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(60)"
        )
        return (
            " ".join(
                (
                    shlex.quote(sys.executable),
                    "-c",
                    shlex.quote(source),
                    shlex.quote(canary),
                )
            )
            + "\n"
        )

    normal_plan = _register_plan(
        config,
        run_id=normal_run_id,
        command=command(normal_marker, normal_canary),
        privacy=None,
    )
    private_plan = _register_plan(
        config,
        run_id=private_run_id,
        command=command(private_marker, private_canary),
        privacy=PROCESS_TITLE_PRIVACY_MODE,
    )
    launched: list[str] = []
    try:
        launched.append(normal_run_id)
        normal_runtime = _launch_locally(normal_plan, home)
        launched.append(private_run_id)
        private_runtime = _launch_locally(private_plan, home)

        normal_pid = _wait_for_pid(normal_marker)
        private_pid = _wait_for_pid(private_marker)
        normal_proc, normal_ps = _process_arguments(normal_pid)
        private_proc, private_ps = _process_arguments(private_pid)

        assert normal_canary.encode() in normal_proc
        assert normal_canary in normal_ps
        assert private_canary.encode() not in private_proc
        assert private_canary not in private_ps
        assert private_run_id.encode() in private_proc
        assert private_run_id in private_ps

        for run_id, runtime, workload_pid in (
            (normal_run_id, normal_runtime, normal_pid),
            (private_run_id, private_runtime, private_pid),
        ):
            pgid = _read_pgid(runtime)
            result = _stop_locally(home, run_id)
            assert result["action"] == "stopped"
            assert not _group_exists(pgid)
            assert not Path(f"/proc/{workload_pid}").exists()
            tmux = subprocess.run(
                ["tmux", "has-session", "-t", f"={run_id}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            assert tmux.returncode != 0
            launched.remove(run_id)
    finally:
        for run_id in launched:
            _force_cleanup(home, run_id)
