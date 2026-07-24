from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from remote_runner._internal import launch, launch_plan, monitoring, registration, stopping
from remote_runner._internal.execution_registry import (
    PROCESS_TITLE_PRIVACY_MODE,
    load_current_run,
    load_yaml,
    process_title_privacy_mode,
    project_paths,
    validate_current_manifest,
)


NORMAL_RUN_ID = "rr-0123456789abcdef"
PRIVATE_RUN_ID = "rr-fedcba9876543210"
pytestmark = pytest.mark.usefixtures("reap_test_runner_processes")


def write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def make_project(
    tmp_path: Path,
    *,
    project_python: str | None = None,
) -> tuple[Path, Path, Path]:
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
    return project, workdir, config


def registration_args(
    config: Path,
    *,
    run_id: str,
    privacy: str | None,
    command: str = "printf 'done\\n'\n",
) -> argparse.Namespace:
    runtime = yaml.safe_load(config.read_text(encoding="utf-8"))["remote"]["local"]
    return argparse.Namespace(
        project_config=config,
        label="process-title privacy test",
        task_id="07-12-remote-process-privacy",
        server="local",
        ssh="local",
        ssh_profile="test",
        configured_cores=8,
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


def register_plan(
    tmp_path: Path,
    *,
    run_id: str,
    privacy: str | None,
    command: str = "printf 'done\\n'\n",
    project_python: str | None = None,
) -> tuple[Path, launch_plan.LaunchPlan]:
    _project, _workdir, config = make_project(
        tmp_path,
        project_python=project_python,
    )
    registration.register(
        registration_args(
            config,
            run_id=run_id,
            privacy=privacy,
            command=command,
        )
    )
    return config, launch_plan.build_launch_plan(project_paths(config), run_id)


def install_plan_assets(plan: launch_plan.LaunchPlan, runtime: Path) -> None:
    runtime.mkdir(mode=0o700)
    for asset in plan.assets:
        path = runtime / asset.name
        path.write_bytes(asset.content)
        path.chmod(asset.mode)


def bootstrap_result(stdout: bytes) -> dict[str, object]:
    prefix = b"RR_BOOTSTRAP_RESULT "
    matches = [
        json.loads(line[len(prefix) :])
        for line in stdout.splitlines()
        if line.startswith(prefix)
    ]
    assert matches, stdout.decode(errors="replace")
    result = matches[-1]
    assert isinstance(result, dict)
    return result


def test_registration_freezes_only_explicit_process_title_privacy(tmp_path: Path) -> None:
    _project, _workdir, config = make_project(tmp_path)
    normal_path = registration.register(
        registration_args(config, run_id=NORMAL_RUN_ID, privacy=None)
    )
    private_path = registration.register(
        registration_args(
            config,
            run_id=PRIVATE_RUN_ID,
            privacy=PROCESS_TITLE_PRIVACY_MODE,
        )
    )

    normal_manifest = load_yaml(normal_path)
    private_manifest = load_yaml(private_path)

    assert "process_title_privacy" not in normal_manifest
    assert process_title_privacy_mode(normal_manifest) is None
    assert private_manifest["process_title_privacy"] == {"mode": "required"}
    assert process_title_privacy_mode(private_manifest) == PROCESS_TITLE_PRIVACY_MODE
    assert load_current_run(project_paths(config), PRIVATE_RUN_ID)[0] == private_manifest

    with pytest.raises(ValueError, match="unsupported privacy mode"):
        registration.register(
            registration_args(
                config,
                run_id="rr-1111222233334444",
                privacy="masked-partial",
            )
        )


@pytest.mark.parametrize(
    "invalid_value",
    [
        "process-title",
        {"mode": "optional"},
        {"mode": "required", "fallback": "normal"},
        [],
    ],
)
def test_manifest_rejects_non_exact_process_title_privacy(
    tmp_path: Path,
    invalid_value: object,
) -> None:
    _project, _workdir, config = make_project(tmp_path)
    manifest_path = registration.register(
        registration_args(
            config,
            run_id=PRIVATE_RUN_ID,
            privacy=PROCESS_TITLE_PRIVACY_MODE,
        )
    )
    manifest = load_yaml(manifest_path)
    manifest["process_title_privacy"] = invalid_value

    with pytest.raises(ValueError, match="must be exactly"):
        validate_current_manifest(manifest)


def test_normal_and_opt_in_plans_have_exact_distinct_assets(tmp_path: Path) -> None:
    normal_root = tmp_path / "normal"
    private_root = tmp_path / "private"
    normal_root.mkdir()
    private_root.mkdir()
    _normal_config, normal = register_plan(
        normal_root,
        run_id=NORMAL_RUN_ID,
        privacy=None,
    )
    _private_config, private = register_plan(
        private_root,
        run_id=PRIVATE_RUN_ID,
        privacy=PROCESS_TITLE_PRIVACY_MODE,
    )

    assert [(asset.name, asset.mode) for asset in normal.assets] == [
        ("run.sh", 0o700),
        ("command.sh", 0o600),
    ]
    assert [(asset.name, asset.mode) for asset in private.assets] == [
        ("run.sh", 0o700),
        ("command.sh", 0o600),
        ("sitecustomize.py", 0o600),
    ]
    assert stat.S_IMODE(private.assets[-1].mode) == 0o600

    normal_public = normal.public()
    private_public = private.public()
    assert "privacy_mode" not in normal_public
    assert private_public["privacy_mode"] == PROCESS_TITLE_PRIVACY_MODE
    assert [item["name"] for item in normal_public["assets"]] == [
        "run.sh",
        "command.sh",
    ]
    assert [item["name"] for item in private_public["assets"]] == [
        "run.sh",
        "command.sh",
        "sitecustomize.py",
    ]
    assert all("content" not in item and "data" not in item for item in private_public["assets"])
    assert normal.bootstrap_ssh_argv[-1] == shlex.join((sys.executable, "-"))
    assert private.bootstrap_ssh_argv[-1] == shlex.join((sys.executable, "-S", "-"))

    normal_bootstrap = normal.bootstrap_stdin.decode()
    normal_wrapper = normal.assets[0].content.decode()
    for privacy_term in ("sitecustomize", "setproctitle", "RR_PROCESS_TITLE"):
        assert privacy_term not in normal_bootstrap
        assert privacy_term not in normal_wrapper

    helper = next(
        asset.content.decode()
        for asset in private.assets
        if asset.name == "sitecustomize.py"
    )
    assert PRIVATE_RUN_ID in helper
    assert "process-title privacy test" not in helper


def test_sitecustomize_probe_discovers_without_executing_existing_hook(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "hook-executed"
    hook = tmp_path / "sitecustomize.py"
    hook.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)

    completed = subprocess.run(
        [sys.executable, "-S", "-c", launch_plan.SITE_CUSTOMIZE_PROBE_SOURCE],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    prefix = "RR_SITE_CUSTOMIZE_PROBE "
    result = next(
        json.loads(line[len(prefix) :])
        for line in completed.stdout.splitlines()
        if line.startswith(prefix)
    )
    assert Path(result).resolve() == hook.resolve()
    assert not marker.exists()


def test_generated_helper_sets_spt_noenv_before_import_and_title(tmp_path: Path) -> None:
    helper_dir = tmp_path / "helper"
    helper_dir.mkdir()
    helper_source = launch_plan._sitecustomize_source(PRIVATE_RUN_ID).decode()
    (helper_dir / "sitecustomize.py").write_text(helper_source, encoding="utf-8")
    import_marker = tmp_path / "import.json"
    title_marker = tmp_path / "title.txt"
    (helper_dir / "setproctitle.py").write_text(
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['IMPORT_MARKER']).write_text(\n"
        "    json.dumps({'spt_noenv': os.environ.get('SPT_NOENV')}),\n"
        "    encoding='utf-8',\n"
        ")\n"
        "_title = None\n"
        "def setproctitle(title):\n"
        "    global _title\n"
        "    _title = title\n"
        "    Path(os.environ['TITLE_MARKER']).write_text(title, encoding='utf-8')\n"
        "def getproctitle():\n"
        "    return _title\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(helper_dir),
            "RR_PROCESS_TITLE_REQUIRED": "1",
            "RR_PROCESS_TITLE": PRIVATE_RUN_ID,
            "IMPORT_MARKER": str(import_marker),
            "TITLE_MARKER": str(title_marker),
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", "pass"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(import_marker.read_text(encoding="utf-8")) == {"spt_noenv": "1"}
    assert title_marker.read_text(encoding="utf-8") == PRIVATE_RUN_ID
    assert helper_source.index('os.environ["SPT_NOENV"] = "1"') < helper_source.index(
        "import setproctitle"
    )


@pytest.mark.parametrize(
    "dependency_source",
    [
        "raise ImportError('dependency unavailable')\n",
        "def setproctitle(_title):\n    raise RuntimeError('cannot set title')\n",
        "def setproctitle(_title):\n    return None\n"
        "def getproctitle():\n    return 'still-visible'\n",
    ],
    ids=["import-failure", "setter-failure", "no-op-setter"],
)
def test_generated_helper_hard_exits_before_workload_on_required_failure(
    tmp_path: Path,
    dependency_source: str,
) -> None:
    helper_dir = tmp_path / "helper"
    helper_dir.mkdir()
    helper_source = launch_plan._sitecustomize_source(PRIVATE_RUN_ID).decode()
    (helper_dir / "sitecustomize.py").write_text(helper_source, encoding="utf-8")
    (helper_dir / "setproctitle.py").write_text(dependency_source, encoding="utf-8")
    continuation = tmp_path / "workload-continued"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(helper_dir),
            "RR_PROCESS_TITLE_REQUIRED": "1",
            "RR_PROCESS_TITLE": PRIVATE_RUN_ID,
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(continuation)!r}).write_text('bad')",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 86
    assert not continuation.exists()
    assert "os._exit(86)" in helper_source


def test_workload_only_injection_prepends_runtime_and_preserves_pythonpath(
    tmp_path: Path,
) -> None:
    original_pythonpath = os.pathsep.join(("/caller/one", "/caller/two"))
    observed = tmp_path / "observed-pythonpath"
    command = f"printf '%s' \"$PYTHONPATH\" > {shlex.quote(str(observed))}\n"
    _config, plan = register_plan(
        tmp_path,
        run_id=PRIVATE_RUN_ID,
        privacy=PROCESS_TITLE_PRIVACY_MODE,
        command=command,
    )
    runtime = tmp_path / "runtime"
    install_plan_assets(plan, runtime)
    env = os.environ.copy()
    env["PYTHONPATH"] = original_pythonpath

    completed = subprocess.run(
        ["bash", str(runtime / "run.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert observed.read_text(encoding="utf-8") == (
        str(runtime) + os.pathsep + original_pythonpath
    )
    private_supervisor = launch_plan._supervisor_source(
        PRIVATE_RUN_ID,
        PROCESS_TITLE_PRIVACY_MODE,
    )
    control_plane, workload_child = private_supervisor.split("if child_pid == 0:", 1)
    assert "RR_PROCESS_TITLE_REQUIRED" not in control_plane
    assert 'workload_env["PYTHONPATH"]' not in control_plane
    assert 'workload_env["RR_PROCESS_TITLE_REQUIRED"] = "1"' in workload_child
    assert 'original_pythonpath = workload_env.get("PYTHONPATH")' in workload_child
    assert "PYTHONPATH" not in launch_plan._supervisor_source(PRIVATE_RUN_ID, None)


def test_privacy_bootstrap_decodes_assets_before_preflight_and_runtime_claim() -> None:
    source = launch_plan._privacy_bootstrap_source()

    assert (
        source.index("decoded = {}")
        < source.index("site_probe = run_privacy_probe(")
        < source.index('decoded["sitecustomize.py"]')
        < source.index("root.mkdir(mode=0o700")
    )
    assert '"sitecustomize.py": 0o600' in source
    assert source.index("site_probe = run_privacy_probe(") < source.index(
        "title_probe = run_privacy_probe("
    )
    assert "process-title privacy conflicts with existing sitecustomize" in source
    assert "generated process-title privacy helper failed" in source


@pytest.mark.parametrize(
    ("site_origin", "title_exit_code", "expected_message"),
    [
        (
            None,
            42,
            "process-title privacy requires importable setproctitle",
        ),
        (
            "/project/sitecustomize.py",
            0,
            "process-title privacy conflicts with existing sitecustomize",
        ),
    ],
    ids=["missing-setproctitle", "existing-sitecustomize"],
)
def test_privacy_preflight_failure_never_starts_tmux_or_claims_runtime(
    tmp_path: Path,
    site_origin: str | None,
    title_exit_code: int,
    expected_message: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tmux_called = tmp_path / "tmux-called"
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(tmux_called))}\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)

    fake_python = tmp_path / "project-python"
    site_result = json.dumps(site_origin)
    fake_python.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "source = sys.argv[-1] if len(sys.argv) > 1 else ''\n"
        "if 'RR_SITE_CUSTOMIZE_PROBE' in source:\n"
        f"    print('RR_SITE_CUSTOMIZE_PROBE ' + {site_result!r})\n"
        "elif 'rr-privacy-preflight' in source:\n"
        f"    raise SystemExit({title_exit_code})\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    project_root = tmp_path / "fixture"
    project_root.mkdir()
    _config, plan = register_plan(
        project_root,
        run_id=PRIVATE_RUN_ID,
        privacy=PROCESS_TITLE_PRIVACY_MODE,
        project_python=str(fake_python),
    )
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")

    completed = subprocess.run(
        [sys.executable, "-"],
        input=plan.bootstrap_stdin,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )

    result = bootstrap_result(completed.stdout)
    assert completed.returncode == 1
    assert result["ok"] is False
    assert result["phase"] == "preflight"
    assert result["tmux_started"] is False
    assert expected_message in str(result["message"])
    assert not (home / ".rr" / PRIVATE_RUN_ID).exists()
    tmux_calls = tmux_called.read_text(encoding="utf-8").splitlines()
    assert tmux_calls == [f"has-session -t ={PRIVATE_RUN_ID}"]
    assert not any("new-session" in call for call in tmux_calls)


def test_privacy_launch_rejection_keeps_registered_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _plan = register_plan(
        tmp_path,
        run_id=PRIVATE_RUN_ID,
        privacy=PROCESS_TITLE_PRIVACY_MODE,
    )
    paths = project_paths(config)

    def rejected(_plan: launch_plan.LaunchPlan, _timeout: int) -> dict[str, object]:
        raise launch.BootstrapRejected("setproctitle is unavailable")

    monkeypatch.setattr(launch, "execute_plan", rejected)
    with pytest.raises(RuntimeError, match="setproctitle is unavailable"):
        launch.launch(paths, PRIVATE_RUN_ID, 1)

    manifest, state = load_current_run(paths, PRIVATE_RUN_ID)
    assert process_title_privacy_mode(manifest) == PROCESS_TITLE_PRIVACY_MODE
    assert state["status"] == "registered"
    assert state["error"] == "setproctitle is unavailable"


def test_monitor_projects_privacy_only_for_opt_in_current_run(tmp_path: Path) -> None:
    _project, _workdir, config = make_project(tmp_path)
    registration.register(
        registration_args(config, run_id=NORMAL_RUN_ID, privacy=None)
    )
    registration.register(
        registration_args(
            config,
            run_id=PRIVATE_RUN_ID,
            privacy=PROCESS_TITLE_PRIVACY_MODE,
        )
    )

    rows = {
        row["run_id"]: row
        for row in monitoring.load_registry_rows(project_paths(config))
    }

    assert "privacy_mode" not in rows[NORMAL_RUN_ID]
    assert rows[PRIVATE_RUN_ID]["privacy_mode"] == PROCESS_TITLE_PRIVACY_MODE
    summary = monitoring.summarize([rows[NORMAL_RUN_ID], rows[PRIVATE_RUN_ID]])
    assert summary.count("privacy=process-title") == 1


def test_process_title_privacy_preserves_stop_owner_and_process_group(
    tmp_path: Path,
) -> None:
    _config, plan = register_plan(
        tmp_path,
        run_id=PRIVATE_RUN_ID,
        privacy=PROCESS_TITLE_PRIVACY_MODE,
        command="sleep 60\n",
    )
    home = tmp_path / "home"
    runtime = home / ".rr" / PRIVATE_RUN_ID
    runtime.parent.mkdir(parents=True)
    install_plan_assets(plan, runtime)
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
        pgid = int((runtime / "pgid").read_text(encoding="utf-8"))

        stopped = subprocess.run(
            [sys.executable, "-"],
            input=stopping.build_stop_stdin(PRIVATE_RUN_ID, 0.5),
            env={**os.environ, "HOME": str(home)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )

        assert stopped.returncode == 0, stopped.stderr.decode(errors="replace")
        wrapper.wait(timeout=10)
        status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
        assert status["state"] == "stopped"
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)
    finally:
        if wrapper.poll() is None:
            wrapper.terminate()
            wrapper.wait(timeout=5)
