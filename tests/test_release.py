from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from remote_runner import __version__
from remote_runner._internal import release
from remote_runner._internal.controller import release_gate
from remote_runner._internal.controller.registry import (
    acquire_dispatch_lease,
    controller_paths,
    controller_scheduler_paths,
    MalformedLeaseError,
    submit_job,
)
from remote_runner._internal.execution_registry import sha256_bytes, write_yaml


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def clean_repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)
    git("config", "user.email", "test@example.com", cwd=repo)
    (repo / "file.txt").write_text("clean\n", encoding="utf-8")
    git("add", "file.txt", cwd=repo)
    git("commit", "-q", "-m", "initial", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


def test_release_source_requires_one_clean_full_revision(tmp_path: Path) -> None:
    repo, revision = clean_repository(tmp_path)

    assert release.resolve_clean_revision(repo) == revision

    (repo / "file.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean and committed"):
        release.resolve_clean_revision(repo)


def test_release_constraints_include_web_extra(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    output = tmp_path / "dist"
    revision = "a" * 40
    calls: list[list[str]] = []

    monkeypatch.setattr(release, "resolve_clean_revision", lambda _repo: revision)

    def extract(_repo: Path, _revision: str, destination: Path) -> None:
        (destination / "src" / "remote_runner").mkdir(parents=True)

    def capture(argv, **_kwargs):
        call = list(argv)
        calls.append(call)
        if call[:2] == ["uv", "build"]:
            (output / "codex_remote_runner-0.1.0-py3-none-any.whl").write_bytes(
                b"wheel"
            )
        elif call[:2] == ["uv", "export"]:
            constraints = Path(call[call.index("--output-file") + 1])
            constraints.write_text("pyyaml==6.0.3\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(release, "_extract_git_archive", extract)
    monkeypatch.setattr(release, "_run", capture)

    release.build_release(repo, output)

    export = next(call for call in calls if call[:2] == ["uv", "export"])
    extras = [
        export[index + 1]
        for index, value in enumerate(export)
        if value == "--extra"
    ]
    assert extras == ["web"]


def test_local_tool_install_is_non_editable_and_constrained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / "codex_remote_runner-0.1.0-py3-none-any.whl"
    constraints = tmp_path / "constraints.txt"
    source = tmp_path / "source.tar.gz"
    payload = tmp_path / "controller-payload.tar.gz"
    for path in (wheel, constraints, source, payload):
        path.write_bytes(b"artifact")
    artifact = release.ReleaseArtifact(
        root=tmp_path,
        revision="a" * 40,
        wheel=wheel,
        constraints=constraints,
        source_archive=source,
        controller_payload=payload,
    )
    calls: list[list[str]] = []
    environments: list[dict[str, str] | None] = []
    tool_dir = tmp_path / "tools"
    bin_dir = tmp_path / "bin"

    def capture(argv, **kwargs):
        calls.append(list(argv))
        environments.append(kwargs.get("env"))
        if list(argv) == ["uv", "tool", "dir"]:
            stdout = str(tool_dir) + "\n"
        elif list(argv) == ["uv", "tool", "dir", "--bin"]:
            stdout = str(bin_dir) + "\n"
        elif list(argv) == [str(bin_dir / "remote-runner"), "--version"]:
            stdout = f"remote-runner 0.1.0 ({artifact.revision})\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(release, "_run", capture)
    release.install_local_tool(artifact)

    assert calls == [
        [
            "uv",
            "tool",
            "install",
            "--force",
            "--python",
            "3.12",
            "--constraints",
            str(constraints),
            f"{wheel}[web]",
        ],
        ["uv", "tool", "dir"],
        ["uv", "tool", "dir", "--bin"],
        [
            str(tool_dir / "codex-remote-runner" / "bin" / "python"),
            "-c",
            (
                "from remote_runner.web_app import STATIC_ROOT; "
                "assert (STATIC_ROOT / 'index.html').is_file()"
            ),
        ],
        [str(bin_dir / "remote-runner"), "--version"],
    ]
    assert "--editable" not in calls[0]
    assert environments[-2] is not None
    assert "PYTHONPATH" not in environments[-2]
    assert environments[-1] is not None
    assert "PYTHONPATH" not in environments[-1]


def test_local_tool_install_rejects_mismatched_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = release.ReleaseArtifact(
        root=tmp_path,
        revision="a" * 40,
        wheel=tmp_path / "runner.whl",
        constraints=tmp_path / "constraints.txt",
        source_archive=tmp_path / "source.tar.gz",
        controller_payload=tmp_path / "payload.tar.gz",
    )

    def capture(argv, **_kwargs):
        if list(argv) == ["uv", "tool", "dir"]:
            stdout = str(tmp_path / "tools")
        elif list(argv) == ["uv", "tool", "dir", "--bin"]:
            stdout = str(tmp_path / "bin")
        elif list(argv)[-1:] == ["--version"]:
            stdout = f"remote-runner 0.1.0 ({'b' * 40})"
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(release, "_run", capture)

    with pytest.raises(RuntimeError, match="local remote-runner revision"):
        release.install_local_tool(artifact)


def test_controller_staging_rejects_mismatched_runtime_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = release.ReleaseArtifact(
        root=tmp_path,
        revision="a" * 40,
        wheel=tmp_path / "runner.whl",
        constraints=tmp_path / "constraints.txt",
        source_archive=tmp_path / "source.tar.gz",
        controller_payload=tmp_path / "payload.tar.gz",
    )
    calls: list[list[str]] = []

    def capture(argv, **_kwargs):
        calls.append(list(argv))
        if list(argv)[0] == "ssh" and "tool dir --bin" in list(argv)[-1]:
            stdout = "/Users/test/.local/bin\n"
        elif list(argv)[0] == "ssh":
            stdout = "/opt/homebrew/bin/uv\n"
        else:
            stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(release, "_run", capture)
    monkeypatch.setattr(
        release,
        "_remote_json",
        lambda *_args, **_kwargs: {"revision": "b" * 40},
    )

    with pytest.raises(RuntimeError, match="controller remote-runner revision"):
        release.stage_controller_release(
            artifact,
            controller_ssh="controller_host",
            controller_root="/Users/test/.remote-runner",
        )

    assert calls[0][-2] == "controller_host"
    assert "/opt/homebrew/bin/uv" in calls[0][-1]
    assert calls[1][0] == "ssh"
    assert "tool dir --bin" in calls[1][-1]
    assert calls[2][0] == "scp"
    assert calls[2][-1].startswith("controller_host:/tmp/remote-runner-")
    assert "SOURCE_REVISION" in calls[3][-1]


def test_controller_venv_entrypoint_survives_activation_move(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    staging = tmp_path / ".release.staging"
    active = tmp_path / "release"
    interpreter = staging / "venv" / "bin" / "python"

    subprocess.run(
        ["uv", "venv", str(staging / "venv"), "--python", "3.12", "--relocatable"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(interpreter),
            "--no-deps",
            str(repo),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    staging.rename(active)

    completed = subprocess.run(
        [str(active / "venv" / "bin" / "remote-runner"), "--version"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert completed.stdout.startswith(f"remote-runner {__version__} (")


def staged_release(controller_root: Path, revision: str) -> Path:
    release_root = controller_root / "runner" / "releases" / revision
    release_root.mkdir(parents=True)
    (release_root / ".deployed-revision").write_text(revision + "\n", encoding="utf-8")
    return release_root


def fake_release_cli(release_root: Path, version: str, revision: str) -> Path:
    executable = release_root / "venv" / "bin" / "remote-runner"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!/bin/sh\nprintf '%s\\n' 'remote-runner {version} ({revision})'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def test_release_activation_stops_dispatch_and_output_sync_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(release_gate, "resolve_tmux_executable", lambda: "tmux")

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(release_gate.subprocess, "run", run)

    assert release_gate._stop_controller_workers(["example"]) == {
        "dispatchers": ["example"],
        "output_sync_workers": ["example"],
    }
    assert calls == [
        ["tmux", "has-session", "-t", "=rr-dispatch-example"],
        ["tmux", "kill-session", "-t", "=rr-dispatch-example"],
        ["tmux", "has-session", "-t", "=rr-output-sync-example"],
        ["tmux", "kill-session", "-t", "=rr-output-sync-example"],
    ]


def test_active_dispatch_lease_blocks_release_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "controller"
    revision = "a" * 40
    monkeypatch.setattr(release_gate, "SOURCE_REVISION", revision)
    staged_release(root, revision)
    paths = controller_paths(root, "example")
    assert acquire_dispatch_lease(
        paths,
        server="compute-a",
        run_id="rr-0123456789abcdef",
        ttl_seconds=120,
    )
    monkeypatch.setattr(
        release_gate,
        "_stop_controller_workers",
        lambda _projects: pytest.fail("dispatcher must not stop while a lease is active"),
    )

    with pytest.raises(RuntimeError, match="active dispatch lease"):
        release_gate.activate_release(root, revision)

    assert not (root / "runner" / "current").exists()


def test_expired_legacy_dispatch_lease_still_blocks_release_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "controller"
    revision = "e" * 40
    monkeypatch.setattr(release_gate, "SOURCE_REVISION", revision)
    staged_release(root, revision)
    command = "python experiment.py"
    submit_job(
        controller_paths(root, "example"),
        {
            "run_id": "rr-0123456789abcdef",
            "revision": "a" * 40,
            "label": "experiment",
            "task_id": "task-1",
            "submitted_command": command,
            "submitted_command_sha256": sha256_bytes(command.encode()),
            "prepared_servers": [
                {
                    "name": "compute-a",
                    "ssh": "compute-a",
                    "ssh_profile": "intranet",
                    "configured_cores": 256,
                    "priority": 100,
                    "bare_repo": "/srv/example/repo.git",
                    "worktree_root": "/srv/example/worktrees",
                    "python": "/opt/example/bin/python3",
                    "output_root": None,
                }
            ],
            "output_relpath": None,
            "output_path": None,
            "output_metadata": {},
        },
    )
    scheduler = controller_scheduler_paths(root)
    scheduler.leases_dir.mkdir(parents=True)
    write_yaml(
        scheduler.leases_dir / "compute-a.yaml",
        {
            "server": "compute-a",
            "project_id": "example",
            "run_id": "rr-0123456789abcdef",
            "kind": "dispatch",
            "created_at": 1000.0,
            "expires_at": 1001.0,
        },
    )

    gate = release_gate.inspect_release_gate(root, now=2000.0)

    assert gate["active_leases"][0]["heartbeat_expired"] is True
    with pytest.raises(RuntimeError, match="active dispatch lease"):
        release_gate.activate_release(root, revision)
    assert not (root / "runner" / "current").exists()
    assert (scheduler.leases_dir / "compute-a.yaml").is_file()


def test_expired_dispatch_lease_without_owner_records_is_cleaned_during_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "controller"
    revision = "d" * 40
    monkeypatch.setattr(release_gate, "SOURCE_REVISION", revision)
    staged_release(root, revision)
    scheduler = controller_scheduler_paths(root)
    scheduler.leases_dir.mkdir(parents=True)
    write_yaml(
        scheduler.leases_dir / "compute-a.yaml",
        {
            "schema_version": 2,
            "server": "compute-a",
            "project_id": "example",
            "run_id": "rr-0123456789abcdef",
            "kind": "dispatch",
            "owner_token": "a" * 64,
            "created_at": 1000.0,
            "heartbeat_at": 1000.5,
            "expires_at": 1001.0,
        },
    )

    result = release_gate.activate_release(root, revision)

    assert [
        lease["run_id"]
        for lease in result["released_orphaned_dispatch_leases"]
    ] == ["rr-0123456789abcdef"]
    assert not (scheduler.leases_dir / "compute-a.yaml").exists()
    assert (root / "runner" / "current").is_symlink()


def test_malformed_lease_blocks_release_inspection_and_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "controller"
    revision = "f" * 40
    monkeypatch.setattr(release_gate, "SOURCE_REVISION", revision)
    staged_release(root, revision)
    scheduler = controller_scheduler_paths(root)
    scheduler.leases_dir.mkdir(parents=True)
    (scheduler.leases_dir / "compute-a.yaml").write_text(
        "expires_at: [broken\n",
        encoding="utf-8",
    )

    with pytest.raises(MalformedLeaseError, match="malformed dispatch lease"):
        release_gate.inspect_release_gate(root)
    with pytest.raises(MalformedLeaseError, match="malformed dispatch lease"):
        release_gate.activate_release(root, revision)
    assert not (root / "runner" / "current").exists()


def test_release_activation_transactionally_aligns_global_and_private_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "controller"
    old_revision = "1" * 40
    revision = "2" * 40
    old_release = staged_release(root, old_revision)
    fake_release_cli(old_release, "0.9.1", old_revision)
    new_release = staged_release(root, revision)
    fake_release_cli(new_release, "0.9.2", revision)
    current = root / "runner" / "current"
    current.symlink_to(Path("releases") / old_revision)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    global_cli = bin_dir / "remote-runner"
    global_cli.write_text(
        f"#!/bin/sh\nprintf '%s\\n' 'remote-runner 0.3.1 ({'3' * 40})'\n",
        encoding="utf-8",
    )
    global_cli.chmod(0o700)
    monkeypatch.setattr(release_gate, "SOURCE_REVISION", revision)

    result = release_gate.activate_release(
        root,
        revision,
        global_cli=global_cli,
    )

    assert current.readlink() == Path("releases") / revision
    assert global_cli.is_symlink()
    assert result["controller_global_cli"]["revision"] == revision
    assert result["controller_private_current"]["revision"] == revision
    assert subprocess.run(
        [str(global_cli), "--version"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip() == f"remote-runner 0.9.2 ({revision})"


def test_release_activation_rolls_back_both_links_on_revision_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "controller"
    old_revision = "4" * 40
    revision = "5" * 40
    old_release = staged_release(root, old_revision)
    fake_release_cli(old_release, "0.9.1", old_revision)
    new_release = staged_release(root, revision)
    fake_release_cli(new_release, "0.9.2", "6" * 40)
    current = root / "runner" / "current"
    current.symlink_to(Path("releases") / old_revision)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    global_cli = bin_dir / "remote-runner"
    old_global = f"remote-runner 0.3.1 ({'3' * 40})"
    global_cli.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{old_global}'\n",
        encoding="utf-8",
    )
    global_cli.chmod(0o700)
    monkeypatch.setattr(release_gate, "SOURCE_REVISION", revision)

    with pytest.raises(RuntimeError, match="private current revision"):
        release_gate.activate_release(root, revision, global_cli=global_cli)

    assert current.readlink() == Path("releases") / old_revision
    assert not global_cli.is_symlink()
    assert subprocess.run(
        [str(global_cli), "--version"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip() == old_global


def test_release_activation_switches_runner_pointer_and_migrates_retired_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "controller"
    revision = "b" * 40
    monkeypatch.setattr(release_gate, "SOURCE_REVISION", revision)
    staged_release(root, revision)
    project = root / "projects" / "example"
    project.mkdir(parents=True)
    evidence = project / "evidence.bin"
    evidence.write_bytes(b"durable-state")
    legacy = project / ".remote-runner" / "experiments"
    legacy.mkdir(parents=True)
    (legacy / "registry.sqlite3").write_bytes(b"opaque-history")
    monkeypatch.setattr(
        release_gate,
        "_stop_controller_workers",
        lambda projects: {
            "dispatchers": list(projects),
            "output_sync_workers": list(projects),
        },
    )

    result = release_gate.activate_release(root, revision)

    current = root / "runner" / "current"
    assert current.is_symlink()
    assert current.readlink() == Path("releases") / revision
    assert result["revision"] == revision
    assert result["projects"] == ["example"]
    assert result["stopped_dispatchers"] == ["example"]
    assert result["stopped_output_sync_workers"] == ["example"]
    assert evidence.read_bytes() == b"durable-state"
    assert legacy.is_file()
    retired = (
        root
        / "retired-state"
        / "experiment-registry-v1"
        / "example"
        / "registry.sqlite3"
    )
    assert retired.read_bytes() == b"opaque-history"
    assert json.loads(legacy.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "status": "retired",
        "destination": str(retired.parent),
    }
    assert result["state_migrations"] == [
        {
            "project_id": "example",
            "output_sync": {"scanned": 0, "migrated_run_ids": []},
            "retired_experiment_registry": {
                "status": "archived",
                "destination": str(retired.parent),
            },
        }
    ]


def test_retired_experiment_registry_migration_is_idempotent(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    source = paths.registry_root / "experiments"
    source.mkdir(parents=True)
    (source / "journal").mkdir()
    (source / "journal" / "event.json").write_bytes(b"opaque-event")

    first = release_gate._archive_legacy_experiment_registry(paths)
    second = release_gate._archive_legacy_experiment_registry(paths)

    destination = (
        paths.root / "retired-state" / "experiment-registry-v1" / "example"
    )
    assert first == {"status": "archived", "destination": str(destination)}
    assert second == {"status": "already_migrated", "destination": str(destination)}
    assert (destination / "journal" / "event.json").read_bytes() == b"opaque-event"
    assert (paths.registry_root / "experiments").is_file()


def test_retired_experiment_registry_migration_rejects_conflict(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    (paths.registry_root / "experiments").mkdir(parents=True)
    destination = (
        paths.root / "retired-state" / "experiment-registry-v1" / "example"
    )
    destination.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="both exist"):
        release_gate._archive_legacy_experiment_registry(paths)


def test_release_activation_rejects_mismatched_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "controller"
    revision = "c" * 40
    monkeypatch.setattr(release_gate, "SOURCE_REVISION", revision)
    release_root = staged_release(root, revision)
    (release_root / ".deployed-revision").write_text("d" * 40, encoding="utf-8")

    with pytest.raises(ValueError, match="receipt does not match"):
        release_gate.activate_release(root, revision)


def test_release_activation_rejects_mismatched_runtime_revision(tmp_path: Path) -> None:
    root = tmp_path / "controller"
    revision = "e" * 40
    staged_release(root, revision)

    with pytest.raises(ValueError, match="runtime revision"):
        release_gate.activate_release(root, revision)

    assert not (root / "runner" / "current").exists()
