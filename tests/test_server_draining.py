from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from remote_runner._internal import server_draining
from remote_runner._internal.execution_registry import write_yaml


def project_config(tmp_path: Path) -> Path:
    path = tmp_path / ".remote-runner.yaml"
    write_yaml(
        path,
        {
            "project_id": "example",
            "controller": {"ssh": "controller_host", "root": "/srv/controller"},
            "source": {"local_repo": "code"},
            "remote": {
                "burst": {
                    "bare_repo": "/srv/repo.git",
                    "worktree_root": "/srv/worktrees",
                    "python": "/opt/python3",
                }
            },
        },
    )
    return path


def test_drain_client_calls_controller_for_configured_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def call(config, action, **kwargs):
        observed.update(config=config, action=action, **kwargs)
        return {"server": "burst", "drained": True}

    monkeypatch.setattr(server_draining, "call_controller", call)
    result = server_draining.update(
        argparse.Namespace(
            project_config=project_config(tmp_path),
            server="burst",
            timeout=8,
        ),
        drained=True,
    )

    assert result["drained"] is True
    assert observed["action"] == "drain-server"
    assert observed["action_args"] == ("--server", "burst")


def test_drain_client_rejects_unconfigured_server(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not configured"):
        server_draining.update(
            argparse.Namespace(
                project_config=project_config(tmp_path),
                server="missing",
                timeout=8,
            ),
            drained=True,
        )


def test_web_drain_adapter_does_not_mutate_shared_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = argparse.Namespace(
        project_config=project_config(tmp_path),
        timeout=8,
    )
    observed: list[tuple[str, bool]] = []

    def update(update_args: argparse.Namespace, *, drained: bool):
        observed.append((update_args.server, drained))
        return {"server": update_args.server, "drained": drained}

    monkeypatch.setattr(server_draining, "update", update)

    result = server_draining.request_server_drain_update(args, "burst", False)

    assert result == {"server": "burst", "drained": False}
    assert observed == [("burst", False)]
    assert not hasattr(args, "server")
