from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat

import pytest

from remote_runner._internal import server_retirement
from remote_runner._internal.config import load_managed_project_config
from remote_runner._internal.execution_registry import load_yaml, write_yaml


def ready_assessment(*_args, **_kwargs) -> dict[str, object]:
    return {
        "schema_version": 1,
        "server": "compute-a",
        "ready": True,
        "drained": False,
        "probe": {"name": "compute-a", "state": "idle", "active_runs": []},
        "projects": [],
        "blockers": [],
        "attention": [],
        "assessed_at": "2026-07-30T00:00:00+00:00",
    }


@pytest.fixture(autouse=True)
def controller_assessment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_retirement, "call_controller", ready_assessment)


def files(tmp_path: Path, *, output_target: str | None = None) -> tuple[Path, Path]:
    project = tmp_path / ".remote-runner.yaml"
    project_document: dict[str, object] = {
        "project_id": "example",
        "controller": {"ssh": "controller", "root": "/srv/controller"},
        "source": {"local_repo": "code"},
        "remote": {
            name: {
                "enabled": True,
                "bare_repo": f"/srv/{name}/repo.git",
                "worktree_root": f"/srv/{name}/worktrees",
                "python": f"/opt/{name}/python3",
                "output_root": f"/srv/{name}/outputs",
            }
            for name in ("compute-a", "compute-b")
        },
        "scheduling": {"testing": {"servers": ["compute-a", "compute-b"]}},
    }
    if output_target is not None:
        project_document["output_sync"] = {
            "target_server": output_target,
            "target_ssh": output_target,
            "target_root": f"/srv/{output_target}/outputs/archive",
            "source_ssh_config": "/srv/archive/ssh.conf",
            "source_hosts": {
                name: f"{name}-source"
                for name in ("compute-a", "compute-b")
                if name != output_target
            },
            "prune_after_sync": {
                "servers": [
                    name for name in ("compute-a", "compute-b") if name != output_target
                ]
            },
        }
    write_yaml(project, project_document)
    registry = tmp_path / "remote-servers.yaml"
    write_yaml(
        registry,
        {
            "servers": {
                "compute-a": {
                    "enabled": True,
                    "ssh": "compute-a",
                    "cores": 8,
                },
                "compute-b": {
                    "enabled": True,
                    "ssh": "compute-b",
                    "cores": 16,
                },
            }
        },
    )
    (tmp_path / "ssh-config").write_text(
        "Host compute-a\n    HostName 192.0.2.10\n    IdentityFile ~/.ssh/shared\n",
        encoding="utf-8",
    )
    (tmp_path / "known_hosts").write_text(
        "192.0.2.10 ssh-ed25519 KEY\n", encoding="utf-8"
    )
    return project, registry


def arguments(project: Path, registry: Path, *, apply: bool) -> argparse.Namespace:
    return argparse.Namespace(
        project_config=project,
        server_registry=registry,
        server="compute-a",
        apply=apply,
        allow_unreachable=False,
        ssh_config=project.parent / "ssh-config",
        known_hosts=project.parent / "known_hosts",
        timeout=8,
    )


def test_retirement_dry_run_reports_changes_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = files(tmp_path)
    before = (project.read_bytes(), registry.read_bytes())
    monkeypatch.setattr(
        server_retirement,
        "update_server_drain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not drain the server")
        ),
    )

    result = server_retirement.retire(arguments(project, registry, apply=False))

    assert result["status"] == "ready_to_retire"
    assert result["applied"] is False
    assert result["cleanup"]["project_config"]["changes"] == [
        {"action": "remove", "field": "remote.compute-a"},
        {"action": "remove", "field": "scheduling.testing.servers"},
    ]
    assert (project.read_bytes(), registry.read_bytes()) == before


def test_retirement_drains_then_removes_configs_and_connection_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = files(tmp_path)
    os.chmod(project, 0o640)
    calls: list[tuple[str, bool]] = []

    def drain(args: argparse.Namespace, *, drained: bool) -> dict[str, object]:
        calls.append((args.server, drained))
        assert "compute-a" in load_yaml(project)["remote"]
        assert "compute-a" in load_yaml(registry)["servers"]
        return {"server": "compute-a", "drained": True, "changed": True}

    monkeypatch.setattr(server_retirement, "update_server_drain", drain)

    result = server_retirement.retire(arguments(project, registry, apply=True))

    assert calls == [("compute-a", True)]
    assert result["status"] == "retired"
    assert result["controller"]["drained"] is True
    project_document = load_yaml(project)
    registry_document = load_yaml(registry)
    assert "compute-a" not in project_document["remote"]
    assert "compute-a" not in registry_document["servers"]
    assert project_document["scheduling"]["testing"]["servers"] == ["compute-b"]
    assert "Host compute-a" not in (tmp_path / "ssh-config").read_text(encoding="utf-8")
    assert "192.0.2.10" not in (tmp_path / "known_hosts").read_text(encoding="utf-8")
    assert stat.S_IMODE(project.stat().st_mode) == 0o640
    load_managed_project_config(project)


def test_retirement_rejects_output_sync_target_before_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = files(tmp_path, output_target="compute-a")
    monkeypatch.setattr(
        server_retirement,
        "update_server_drain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("blocked retirement must not drain")
        ),
    )

    with pytest.raises(ValueError, match="move output_sync.target_server first"):
        server_retirement.retire(arguments(project, registry, apply=True))


def test_retirement_rejects_last_automatic_candidate(tmp_path: Path) -> None:
    project, registry = files(tmp_path)
    raw = load_yaml(project)
    raw["remote"]["compute-b"]["auto_select"] = False
    write_yaml(project, raw)

    with pytest.raises(ValueError, match="at least one enabled automatic candidate"):
        server_retirement.retire(arguments(project, registry, apply=False))


def test_retirement_removes_already_disabled_config_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = files(tmp_path)
    project_raw = load_yaml(project)
    project_raw["remote"]["compute-a"]["enabled"] = False
    project_raw["scheduling"]["testing"]["servers"] = ["compute-b"]
    write_yaml(project, project_raw)
    registry_raw = load_yaml(registry)
    registry_raw["servers"]["compute-a"]["enabled"] = False
    write_yaml(registry, registry_raw)
    monkeypatch.setattr(
        server_retirement,
        "update_server_drain",
        lambda *_args, **_kwargs: {"drained": True, "changed": False},
    )

    result = server_retirement.retire(arguments(project, registry, apply=True))

    assert result["cleanup"]["project_config"]["changes"][0] == {
        "action": "remove",
        "field": "remote.compute-a",
    }
    assert result["status"] == "retired"


def test_retirement_reports_safe_partial_state_when_registry_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = files(tmp_path)
    before = (project.read_bytes(), registry.read_bytes())
    monkeypatch.setattr(
        server_retirement,
        "update_server_drain",
        lambda *_args, **_kwargs: {"drained": True, "changed": True},
    )
    monkeypatch.setattr(
        server_retirement,
        "replace_config_yaml",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(RuntimeError, match="is drained and connection credentials"):
        server_retirement.retire(arguments(project, registry, apply=True))

    assert (project.read_bytes(), registry.read_bytes()) == before


def test_retirement_rejects_one_file_used_for_both_config_roles(
    tmp_path: Path,
) -> None:
    project, _registry = files(tmp_path)
    retire_args = arguments(project, project, apply=False)

    with pytest.raises(ValueError, match="must be different files"):
        server_retirement.retire(retire_args)


def test_retirement_blockers_prevent_drain_and_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = files(tmp_path)
    blocked = ready_assessment()
    blocked["ready"] = False
    blocked["blockers"] = [
        {"code": "active_execution", "run_id": "rr-0123456789abcdef"}
    ]
    monkeypatch.setattr(
        server_retirement, "call_controller", lambda *_args, **_kwargs: blocked
    )
    monkeypatch.setattr(
        server_retirement,
        "update_server_drain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("blocked retirement must not drain")
        ),
    )

    preview = server_retirement.retire(arguments(project, registry, apply=False))
    assert preview["ready"] is False
    assert preview["status"] == "blocked"

    with pytest.raises(server_retirement.RetirementBlockedError):
        server_retirement.retire(arguments(project, registry, apply=True))


def test_retirement_allow_unreachable_is_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = files(tmp_path)
    unreachable = ready_assessment()
    unreachable["ready"] = False
    unreachable["blockers"] = [
        {"code": "server_unreachable", "detail": "connection refused"}
    ]
    monkeypatch.setattr(
        server_retirement, "call_controller", lambda *_args, **_kwargs: unreachable
    )
    monkeypatch.setattr(
        server_retirement,
        "update_server_drain",
        lambda *_args, **_kwargs: {"drained": True, "changed": True},
    )
    retire_args = arguments(project, registry, apply=True)
    retire_args.allow_unreachable = True

    result = server_retirement.retire(retire_args)

    assert result["status"] == "retired"
    assert result["assessment"]["effective_blockers"] == []


def test_retirement_cleans_archive_alias_and_revokes_source_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = files(tmp_path, output_target="compute-b")
    calls: list[bool] = []

    def archive(
        _config,
        source_server: str,
        *,
        apply: bool,
        timeout: int,
        expected_digest: str | None = None,
    ):
        assert source_server == "compute-a"
        assert timeout == 8
        calls.append(apply)
        return {
            "status": "inspected",
            "applied": apply,
            "public_keys": ["ssh-ed25519 PUBLIC"],
            "state_digest": expected_digest or "sha256:" + "a" * 64,
        }

    revoked: list[tuple[str, list[object]]] = []
    monkeypatch.setattr(server_retirement, "inspect_or_apply_archive_source", archive)
    monkeypatch.setattr(
        server_retirement,
        "_revoke_source_keys",
        lambda alias, keys, **_kwargs: (
            revoked.append((alias, keys)) or {"status": "revoked", "removed": 1}
        ),
    )
    monkeypatch.setattr(
        server_retirement,
        "update_server_drain",
        lambda *_args, **_kwargs: {"drained": True, "changed": True},
    )

    result = server_retirement.retire(arguments(project, registry, apply=True))

    assert calls == [False, False, True]
    assert revoked == [("compute-a", ["ssh-ed25519 PUBLIC"])]
    assert result["source_authorization"]["removed"] == 1
    output_sync = load_yaml(project)["output_sync"]
    assert "compute-a" not in output_sync["source_hosts"]
    assert "compute-a" not in output_sync["prune_after_sync"]["servers"]
