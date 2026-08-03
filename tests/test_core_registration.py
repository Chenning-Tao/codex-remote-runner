from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from remote_runner._internal import pool, registration, remote_shell
from remote_runner._internal.config import load_managed_project_config
from remote_runner._internal.execution_registry import (
    CURRENT_MANIFEST_SCHEMA,
    CURRENT_STATE_SCHEMA,
    load_current_run,
    load_yaml,
    project_paths,
    resolve_project_config,
    sha256_bytes,
    validate_current_manifest,
    validate_current_state,
)


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
RUN_ID = "rr-0123456789abcdef"


def write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def make_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    config = root / ".remote-runner.yaml"
    write_yaml(
        config,
        {
            "remote": {
                "compute-a": {
                    "workdir": "/srv/project/code",
                    "python": "/srv/envs/project/bin/python3",
                },
                "compute-b": {
                    "workdir": "/srv/project/code",
                    "python": "/srv/envs/project/bin/python3",
                },
            }
        },
    )
    return root, config


def make_managed_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "managed-project"
    root.mkdir()
    config = root / ".remote-runner.yaml"
    write_yaml(
        config,
        {
            "project_id": "managed-project",
            "controller": {"ssh": "controller_host", "root": "/Users/test/.remote-runner"},
            "source": {"local_repo": "code"},
            "remote": {
                "compute-a": {
                    "bare_repo": "/srv/project/repo.git",
                    "worktree_root": "/srv/project/worktrees",
                    "python": "/srv/envs/project/bin/python3",
                },
                "compute-b": {
                    "bare_repo": "/srv/project/repo.git",
                    "worktree_root": "/srv/project/worktrees",
                    "python": "/srv/envs/project/bin/python3",
                },
            },
        },
    )
    return root, config


def registration_args(config: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "project_config": config,
        "label": "diagnostic",
        "task_id": "task-1",
        "server": "compute-a",
        "ssh": "user@compute-a",
        "ssh_profile": "intranet",
        "configured_cores": 256,
        "minimum_cores": 128,
        "assigned_cores": 48,
        "command": "printf 'hello world\\n'\nprintf 'done\\n'\n",
        "remote_workdir": "/srv/project/code",
        "project_python": "/srv/envs/project/bin/python3",
        "expected_revision": "abc123",
        "require_clean_worktree": True,
        "output_path": "/srv/project/results/run.json",
        "output_metadata": '{"kind":"diagnostic"}',
        "run_id": RUN_ID,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_project_config_discovery_anchors_one_registry(tmp_path: Path) -> None:
    root, config = make_project(tmp_path)
    nested = root / "code" / ".worktrees" / "feature" / "src"
    nested.mkdir(parents=True)

    from_root = project_paths(resolve_project_config(start=root))
    from_nested = project_paths(resolve_project_config(start=nested))
    from_explicit = project_paths(resolve_project_config(config, start=tmp_path))

    assert from_root == from_nested == from_explicit
    assert from_root.project_root == root.resolve()
    assert from_root.registry_root == root.resolve() / ".remote-runner"


def test_missing_project_config_fails_instead_of_guessing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="could not find"):
        resolve_project_config(start=tmp_path)


def test_explicit_server_never_probes_another_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, project_config = make_managed_project(tmp_path)
    registry = tmp_path / "servers.yaml"
    write_yaml(
        registry,
        {
            "servers": {
                "compute-a": {"ssh": "compute-a", "cores": 256, "enabled": True},
                "compute-b": {"ssh": "compute-b", "cores": 128, "enabled": True},
            }
        },
    )
    calls: list[str] = []

    def unreachable(ssh: str, _timeout: int) -> dict[str, object]:
        calls.append(ssh)
        return {"reachable": False, "error": "offline"}

    monkeypatch.setattr(pool, "probe_endpoint", unreachable)
    candidates = pool.probe_project_pool(
        load_managed_project_config(project_config),
        registry,
        explicit_server="compute-a",
        ssh_profile="auto",
        timeout=8,
    )

    assert calls == ["compute-a"]
    assert [item["name"] for item in candidates] == ["compute-a"]
    assert candidates[0]["probe"]["reachable"] is False


def test_server_probe_sends_one_quoted_remote_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def completed(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="loadavg=1.0 2.0 3.0 1/1 1\nnproc=8\nmem_available_kb=1024\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", completed)
    result = pool.probe_endpoint("example", 8)

    assert result["reachable"] is True
    assert observed[0][-2] == "example"
    assert observed[0][-1].startswith("sh -c ")
    assert observed[0][-1] == "sh -c true"


def test_server_probe_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="probe timeout must be positive"):
        pool.probe_endpoint("example", 0)


def test_all_server_selection_normalizes_to_automatic_pool() -> None:
    assert pool.normalize_explicit_server("all") is None
    assert pool.normalize_explicit_server(None) is None
    assert pool.normalize_explicit_server("compute-a") == "compute-a"


@pytest.mark.parametrize("value", ("", "  ", 7))
def test_server_selection_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="non-empty server name or 'all'"):
        pool.normalize_explicit_server(value)


def test_remote_capture_does_not_source_login_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def completed(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(remote_shell.subprocess, "run", completed)
    code, stdout, stderr = remote_shell.ssh_capture("example", "printf ok", 8)

    assert (code, stdout, stderr) == (0, "ok", "")
    assert observed[0][-1].startswith("bash -c ")
    assert "bash -lc" not in observed[0][-1]


def test_local_pool_probe_does_not_preselect_a_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, project_config = make_managed_project(tmp_path)
    registry = tmp_path / "servers.yaml"
    write_yaml(
        registry,
        {
            "servers": {
                "compute-a": {"ssh": "compute-a", "cores": 256, "priority": 10},
                "compute-b": {"ssh": "compute-b", "cores": 32, "priority": 100},
            }
        },
    )

    monkeypatch.setattr(
        pool,
        "probe_endpoint",
        lambda _ssh, _timeout: {"reachable": True},
    )
    candidates = pool.probe_project_pool(
        load_managed_project_config(project_config),
        registry,
        explicit_server=None,
        ssh_profile="auto",
        timeout=8,
    )

    assert [item["name"] for item in candidates] == ["compute-a", "compute-b"]
    assert all("selected" not in item for item in candidates)
    assert all("load5" not in item["probe"] for item in candidates)


def test_pool_probes_only_servers_meeting_minimum_cores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, project_config = make_managed_project(tmp_path)
    registry = tmp_path / "servers.yaml"
    write_yaml(
        registry,
        {
            "servers": {
                "compute-a": {"ssh": "compute-a", "cores": 256},
                "compute-b": {"ssh": "compute-b", "cores": 128},
            }
        },
    )
    calls: list[str] = []

    def reachable(ssh: str, _timeout: int) -> dict[str, object]:
        calls.append(ssh)
        return {"reachable": True}

    monkeypatch.setattr(pool, "probe_endpoint", reachable)
    candidates = pool.probe_project_pool(
        load_managed_project_config(project_config),
        registry,
        explicit_server=None,
        ssh_profile="auto",
        timeout=8,
        minimum_cores=256,
    )

    assert calls == ["compute-a"]
    assert [item["name"] for item in candidates] == ["compute-a"]


def test_pool_probes_only_allowed_candidate_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, project_config = make_managed_project(tmp_path)
    raw_config = load_yaml(project_config)
    raw_config["remote"]["compute-c"] = {
        "bare_repo": "/srv/repo.git",
        "worktree_root": "/srv/worktrees",
        "python": "/opt/python3",
    }
    write_yaml(project_config, raw_config)
    registry = tmp_path / "servers.yaml"
    write_yaml(
        registry,
        {
            "servers": {
                "compute-b": {"ssh": "compute-b", "cores": 128},
                "compute-a": {"ssh": "compute-a", "cores": 256},
                "compute-c": {"ssh": "compute-c", "cores": 32},
            }
        },
    )
    probed: list[str] = []

    def probe(ssh: str, _timeout: int) -> dict[str, bool]:
        probed.append(ssh)
        return {"reachable": True}

    monkeypatch.setattr(pool, "probe_endpoint", probe)

    candidates = pool.probe_project_pool(
        load_managed_project_config(project_config),
        registry,
        explicit_server=None,
        ssh_profile="auto",
        timeout=8,
        candidate_servers=("compute-b", "compute-a", "compute-c"),
    )

    assert [item["name"] for item in candidates] == ["compute-b", "compute-a", "compute-c"]
    assert probed == ["compute-b", "compute-a", "compute-c"]


def test_pool_carries_global_testing_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, project_config = make_managed_project(tmp_path)
    registry = tmp_path / "servers.yaml"
    write_yaml(
        registry,
        {
            "servers": {
                "compute-a": {"ssh": "compute-a", "cores": 256, "testing": {"slots": 1}},
                "compute-b": {"ssh": "compute-b", "cores": 128},
            }
        },
    )
    monkeypatch.setattr(pool, "probe_endpoint", lambda *_args: {"reachable": True})

    candidates = pool.probe_project_pool(
        load_managed_project_config(project_config),
        registry,
        explicit_server=None,
        ssh_profile="auto",
        timeout=8,
    )

    assert {item["name"]: item["test_slots"] for item in candidates} == {
        "compute-b": 0,
        "compute-a": 1,
    }


def test_explicit_server_below_minimum_cores_fails_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, project_config = make_managed_project(tmp_path)
    registry = tmp_path / "servers.yaml"
    write_yaml(
        registry,
        {
            "servers": {
                "compute-a": {"ssh": "compute-a", "cores": 256},
                "compute-b": {"ssh": "compute-b", "cores": 128},
            }
        },
    )
    monkeypatch.setattr(
        pool,
        "probe_endpoint",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not probe")),
    )

    with pytest.raises(ValueError, match="below required minimum 256"):
        pool.probe_project_pool(
            load_managed_project_config(project_config),
            registry,
            explicit_server="compute-b",
            ssh_profile="auto",
            timeout=8,
            minimum_cores=256,
        )


def test_registration_round_trips_exact_command_and_core_schema(tmp_path: Path) -> None:
    root, config = make_project(tmp_path)
    args = registration_args(config)
    manifest_path = registration.register(args)
    paths = project_paths(config)
    manifest, state = load_current_run(paths, RUN_ID)
    command_path = manifest_path.parent / "command.sh"

    assert manifest_path == root / ".remote-runner" / "runs" / RUN_ID / "manifest.yaml"
    assert command_path.read_bytes() == args.command.encode("utf-8")
    assert manifest["command"] == args.command
    assert manifest["command_sha256"] == sha256_bytes(args.command.encode("utf-8"))
    assert manifest["schema_version"] == CURRENT_MANIFEST_SCHEMA
    assert state["state_schema_version"] == CURRENT_STATE_SCHEMA
    assert state["status"] == "registered"
    assert state["revision"] == 0
    assert manifest["assigned_cores"] == 48
    assert manifest["minimum_cores"] == 128
    assert "process_privacy" not in manifest
    assert "assets" not in manifest
    assert "launch_plan" not in manifest
    assert not any(path.name == "sitecustomize.py" for path in manifest_path.parent.rglob("*"))

    for path in (manifest_path, manifest_path.parent / "state.yaml", command_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / ".remote-runner").stat().st_mode) == 0o700


def test_registration_rejects_server_below_minimum_cores(tmp_path: Path) -> None:
    _root, config = make_project(tmp_path)

    with pytest.raises(ValueError, match="does not satisfy minimum cores"):
        registration.register(registration_args(config, minimum_cores=512))


def test_manifest_identity_must_match_registry_path(tmp_path: Path) -> None:
    _root, config = make_project(tmp_path)
    manifest_path = registration.register(registration_args(config))
    manifest = load_yaml(manifest_path)
    manifest["run_id"] = "rr-fedcba9876543210"
    write_yaml(manifest_path, manifest)

    with pytest.raises(ValueError, match="manifest run_id does not match"):
        load_current_run(project_paths(config), RUN_ID)


def test_registration_records_assigned_cores_without_rewriting_command(tmp_path: Path) -> None:
    _root, config = make_project(tmp_path)
    command = "python experiment.py --num-workers 7\n"
    manifest_path = registration.register(
        registration_args(config, command=command, assigned_cores=7)
    )

    assert (manifest_path.parent / "command.sh").read_text(encoding="utf-8") == command
    assert load_yaml(manifest_path)["assigned_cores"] == 7


def test_registration_records_portable_output_identity(tmp_path: Path) -> None:
    _root, config = make_project(tmp_path)
    manifest_path = registration.register(
        registration_args(
            config,
            output_root="/home/user/project root",
            output_relpath="validation/run with spaces/result.json",
            output_path=(
                "/home/user/project root/validation/run with spaces/result.json"
            ),
        )
    )

    manifest = load_yaml(manifest_path)
    assert manifest["output_root"] == "/home/user/project root"
    assert manifest["output_relpath"] == "validation/run with spaces/result.json"
    assert manifest["output_path"] == (
        "/home/user/project root/validation/run with spaces/result.json"
    )


def test_schema_rejects_privacy_fields_and_observation_states(tmp_path: Path) -> None:
    _root, config = make_project(tmp_path)
    manifest_path = registration.register(registration_args(config))
    manifest = load_yaml(manifest_path)
    state = load_yaml(manifest_path.parent / "state.yaml")

    manifest["process_privacy"] = {"effective_level": "masked_partial"}
    with pytest.raises(ValueError, match="deferred fields"):
        validate_current_manifest(manifest)

    state["status"] = "unreachable"
    with pytest.raises(ValueError, match="authoritative status"):
        validate_current_state(state, RUN_ID)


def test_concurrent_registration_never_overwrites_existing_run(tmp_path: Path) -> None:
    _root, config = make_project(tmp_path)
    register_code = """
import argparse
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from remote_runner._internal import registration

args = argparse.Namespace(
    project_config=Path(sys.argv[2]),
    label=sys.argv[3],
    task_id="task-1",
    server="compute-a",
    ssh="compute-a",
    ssh_profile="auto",
    configured_cores=256,
    assigned_cores=256,
    command=sys.argv[4],
    remote_workdir="/srv/project/code",
    project_python="/srv/envs/project/bin/python3",
    expected_revision=None,
    require_clean_worktree=False,
    output_path=None,
    output_metadata=None,
    run_id="rr-0123456789abcdef",
)
try:
    print(registration.register(args))
except Exception as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(2)
"""
    base = [
        sys.executable,
        "-c",
        register_code,
        str(SRC_DIR),
        str(config),
    ]
    first = subprocess.Popen(
        [*base, "first", "printf first\\n"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        [*base, "second", "printf second\\n"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_out, first_err = first.communicate(timeout=20)
    second_out, second_err = second.communicate(timeout=20)

    assert sorted([first.returncode, second.returncode]) == [0, 2], (
        first_out,
        first_err,
        second_out,
        second_err,
    )
    paths = project_paths(config)
    manifest, _state = load_current_run(paths, RUN_ID)
    command_bytes = (paths.runs_dir / RUN_ID / "command.sh").read_bytes()
    assert command_bytes in {b"printf first\\n", b"printf second\\n"}
    assert command_bytes == manifest["command"].encode("utf-8")
    assert manifest["command_sha256"] == sha256_bytes(command_bytes)
    assert len(list(paths.runs_dir.glob(f".{RUN_ID}.*"))) == 0


def test_project_runtime_requires_absolute_paths(tmp_path: Path) -> None:
    _root, config = make_project(tmp_path)
    with pytest.raises(ValueError, match="absolute POSIX path"):
        registration.register(registration_args(config, remote_workdir="~/project"))


def test_registry_files_are_private_even_under_permissive_umask(tmp_path: Path) -> None:
    _root, config = make_project(tmp_path)
    old_umask = os.umask(0)
    try:
        manifest_path = registration.register(registration_args(config))
    finally:
        os.umask(old_umask)

    paths = project_paths(config)
    assert stat.S_IMODE(paths.registry_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.runs_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.locks_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.events_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
