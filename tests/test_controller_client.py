from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from remote_runner._internal.config import load_managed_project_config
from remote_runner._internal.controller import client as controller_client
from remote_runner._internal.execution_registry import write_yaml


def test_controller_call_uses_batch_ssh_and_private_stdin(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / ".remote-runner.yaml"
    write_yaml(
        path,
        {
            "project_id": "example",
            "controller": {
                "ssh": "controller_host",
                "root": "/Users/test/.remote-runner",
            },
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
    config = load_managed_project_config(path)
    observed: dict[str, object] = {}

    def completed(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(argv=argv, input=kwargs["input"], timeout=kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", completed)
    result = controller_client.call_controller(
        config,
        "submit",
        timeout=8,
        payload={"command": "python experiment.py --secret value"},
    )

    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[:4] == ["ssh", "-o", "BatchMode=yes", "-o"]
    assert "ControlMaster=auto" in argv
    assert "ControlPersist=60" in argv
    assert "ControlPath=~/.ssh/remote-runner-%C" in argv
    assert argv[-2] == "controller_host"
    assert "/Users/test/.remote-runner/runner/current/venv/bin/python" in argv[-1]
    assert "-m remote_runner._internal.controller" in argv[-1]
    assert "python experiment.py" not in argv[-1]
    assert json.loads(str(observed["input"]))["command"].endswith("--secret value")
    assert observed["timeout"] == 128
    assert result == {"ok": True}


def test_controller_timeout_is_reported_as_runtime_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / ".remote-runner.yaml"
    write_yaml(
        path,
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
    config = load_managed_project_config(path)

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["ssh"], 128)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(RuntimeError, match="controller status timed out"):
        controller_client.call_controller(config, "status", timeout=8)


def test_unsupported_controller_action_has_a_distinct_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    config = load_managed_project_config(path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["ssh"],
            2,
            stdout="",
            stderr="argument action: invalid choice: 'wait-runs'",
        ),
    )

    with pytest.raises(
        controller_client.ControllerActionUnsupportedError,
        match="does not support action 'wait-runs'",
    ):
        controller_client.call_controller(config, "wait-runs", timeout=8)


def test_bounded_long_poll_enables_ssh_keepalive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    config = load_managed_project_config(path)
    observed: dict[str, object] = {}

    def completed(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed.update(argv=argv, timeout=kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", completed)
    controller_client.call_controller(
        config,
        "wait-run",
        timeout=8,
        overall_timeout=68,
    )

    argv = observed["argv"]
    assert isinstance(argv, list)
    assert "ServerAliveInterval=15" in argv
    assert "ServerAliveCountMax=4" in argv
    assert observed["timeout"] == 68


def test_controller_sandbox_denial_is_not_reported_as_alias_bypass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / ".remote-runner.yaml"
    write_yaml(
        path,
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
    config = load_managed_project_config(path)
    stderr = "ssh: connect to host 100.100.212.72 port 22: Operation not permitted"
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["ssh"],
            255,
            stdout="",
            stderr=stderr,
        ),
    )

    with pytest.raises(RuntimeError) as error:
        controller_client.call_controller(config, "status", timeout=8)

    message = str(error.value)
    assert "local Codex network sandbox" in message
    assert "network-enabled approval" in message
    assert "resolved from 'controller_host'" in message
    assert "does not mean the SSH alias or config was bypassed" in message
    assert stderr in message


def test_controller_transport_error_is_unchanged_outside_codex_sandbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / ".remote-runner.yaml"
    write_yaml(
        path,
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
    config = load_managed_project_config(path)
    stderr = "ssh: connect to host 100.100.212.72 port 22: Connection timed out"
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["ssh"],
            255,
            stdout="",
            stderr=stderr,
        ),
    )

    with pytest.raises(RuntimeError, match="Connection timed out") as error:
        controller_client.call_controller(config, "status", timeout=8)

    assert "local Codex network sandbox" not in str(error.value)
