from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from remote_runner._internal.config import load_managed_project_config
from remote_runner._internal.controller.layout import controller_release_layout
from remote_runner._internal.execution_registry import load_yaml, write_yaml


def test_load_yaml_prefers_the_safe_c_loader_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document.yaml"
    path.write_text("value: 1\n", encoding="utf-8")
    original_load = yaml.load
    loaders: list[type[object]] = []

    def capture_loader(stream, *, Loader):
        loaders.append(Loader)
        return original_load(stream, Loader=Loader)

    monkeypatch.setattr(yaml, "load", capture_loader)

    assert load_yaml(path) == {"value": 1}
    assert loaders == [getattr(yaml, "CSafeLoader", yaml.SafeLoader)]


def managed_config(tmp_path: Path) -> Path:
    config = tmp_path / ".remote-runner.yaml"
    write_yaml(
        config,
        {
            "project_id": "example",
            "controller": {
                "ssh": "controller_host",
                "root": "/Users/test/.remote-runner",
            },
            "source": {"local_repo": "code"},
            "remote": {
                "compute-a": {
                    "bare_repo": "/srv/example/repo.git",
                    "worktree_root": "/srv/example/worktrees",
                    "python": "/opt/example/bin/python3",
                    "output_root": "/home/user-a/example",
                },
                "archive": {
                    "auto_select": False,
                    "bare_repo": "/srv/example/repo.git",
                    "worktree_root": "/srv/example/worktrees",
                    "python": "/opt/example/bin/python3",
                    "output_root": "/home/other-user/example",
                },
            },
            "scheduling": {
                "strategy": "max_available_cores",
                "lease_seconds": 90,
                "probe_interval_seconds": 10,
                "testing": {"servers": ["archive", "compute-a"]},
            },
        },
    )
    return config


def test_loads_managed_config_and_separates_explicit_pool(tmp_path: Path) -> None:
    config = load_managed_project_config(managed_config(tmp_path))

    assert config.project_id == "example"
    assert config.controller.ssh == "controller_host"
    assert config.controller.root == "/Users/test/.remote-runner"
    assert config.local_repo == (tmp_path / "code").resolve()
    assert config.candidate_names() == ["compute-a"]
    assert config.candidate_names("archive") == ["archive"]
    assert config.remotes["compute-a"].worktree_for_revision("a" * 40) == (
        "/srv/example/worktrees/" + "a" * 40
    )
    assert config.remotes["compute-a"].output_root == "/home/user-a/example"
    assert config.remotes["archive"].output_root == "/home/other-user/example"
    assert config.scheduling.lease_seconds == 90
    assert config.scheduling.testing_servers == ("archive", "compute-a")
    assert config.output_sync is None


def test_loads_default_all_succeeded_output_sync_config(tmp_path: Path) -> None:
    path = managed_config(tmp_path)
    raw = load_yaml(path)
    raw["output_sync"] = {
        "target_server": "archive",
        "target_ssh": "archive",
        "target_root": "/home/other-user/example/archive/scientific-v1",
        "source_ssh_config": "/home/other-user/.ssh/output-sync.conf",
        "source_hosts": {"compute-a": "compute-a-int"},
        "retry_seconds": 30,
    }
    write_yaml(path, raw)

    config = load_managed_project_config(path)

    assert config.output_sync is not None
    assert config.output_sync.target_server == "archive"
    assert config.output_sync.target_python == "/opt/example/bin/python3"
    assert config.output_sync.source_hosts == {"compute-a": "compute-a-int"}
    assert config.output_sync.prune_source_servers == ()
    assert config.output_sync.restricted_source_keys is False
    assert config.output_sync.retry_seconds == 30
    assert config.output_sync.paused is False


def test_loads_restricted_output_source_keys(tmp_path: Path) -> None:
    path = managed_config(tmp_path)
    raw = load_yaml(path)
    raw["output_sync"] = {
        "target_server": "archive",
        "target_ssh": "archive",
        "target_root": "/home/other-user/example/archive/scientific-v1",
        "source_ssh_config": "/home/other-user/.ssh/output-sync.conf",
        "source_hosts": {"compute-a": "compute-a-int"},
        "restricted_source_keys": True,
    }
    write_yaml(path, raw)

    config = load_managed_project_config(path)

    assert config.output_sync is not None
    assert config.output_sync.restricted_source_keys is True


def test_loads_post_sync_source_pruning_allow_list(tmp_path: Path) -> None:
    path = managed_config(tmp_path)
    raw = load_yaml(path)
    raw["output_sync"] = {
        "target_server": "archive",
        "target_ssh": "archive",
        "target_root": "/home/other-user/example/archive/scientific-v1",
        "source_ssh_config": "/home/other-user/.ssh/output-sync.conf",
        "source_hosts": {"compute-a": "compute-a-int"},
        "prune_after_sync": {"servers": ["compute-a"]},
    }
    write_yaml(path, raw)

    config = load_managed_project_config(path)

    assert config.output_sync is not None
    assert config.output_sync.prune_source_servers == ("compute-a",)


def test_post_sync_pruning_rejects_non_source_server(tmp_path: Path) -> None:
    path = managed_config(tmp_path)
    raw = load_yaml(path)
    raw["output_sync"] = {
        "target_server": "archive",
        "target_ssh": "archive",
        "target_root": "/home/other-user/example/archive/scientific-v1",
        "source_ssh_config": "/home/other-user/.ssh/output-sync.conf",
        "source_hosts": {"compute-a": "compute-a-int"},
        "prune_after_sync": {"servers": ["archive"]},
    }
    write_yaml(path, raw)

    with pytest.raises(ValueError, match="must name configured source hosts"):
        load_managed_project_config(path)


def test_output_sync_requires_every_enabled_output_source(tmp_path: Path) -> None:
    path = managed_config(tmp_path)
    raw = load_yaml(path)
    raw["output_sync"] = {
        "target_server": "archive",
        "target_ssh": "archive",
        "target_root": "/home/other-user/example/archive",
        "source_ssh_config": "/home/other-user/.ssh/output-sync.conf",
        "source_hosts": {},
    }
    write_yaml(path, raw)

    with pytest.raises(ValueError, match="missing: compute-a"):
        load_managed_project_config(path)


def test_output_sync_allows_disabled_source_for_in_flight_archival(
    tmp_path: Path,
) -> None:
    path = managed_config(tmp_path)
    raw = load_yaml(path)
    raw["remote"]["burst"] = {
        "enabled": False,
        "auto_select": False,
        "bare_repo": "/srv/cloud/repo.git",
        "worktree_root": "/srv/cloud/worktrees",
        "python": "/opt/cloud/python3",
        "output_root": "/srv/cloud/output",
    }
    raw["output_sync"] = {
        "target_server": "archive",
        "target_ssh": "archive",
        "target_root": "/home/other-user/example/archive",
        "source_ssh_config": "/home/other-user/.ssh/output-sync.conf",
        "source_hosts": {
            "compute-a": "compute-a-int",
            "burst": "burst-int",
        },
    }
    write_yaml(path, raw)

    config = load_managed_project_config(path)

    assert config.output_sync is not None
    assert config.output_sync.source_hosts["burst"] == "burst-int"


def test_output_sync_rejects_unknown_source_alias(tmp_path: Path) -> None:
    path = managed_config(tmp_path)
    raw = load_yaml(path)
    raw["output_sync"] = {
        "target_server": "archive",
        "target_ssh": "archive",
        "target_root": "/home/other-user/example/archive",
        "source_ssh_config": "/home/other-user/.ssh/output-sync.conf",
        "source_hosts": {"compute-a": "compute-a-int", "missing": "missing-int"},
    }
    write_yaml(path, raw)

    with pytest.raises(ValueError, match="unknown: missing"):
        load_managed_project_config(path)


def test_rejects_testing_server_outside_enabled_project_remotes(tmp_path: Path) -> None:
    config = managed_config(tmp_path)
    raw = load_yaml(config)
    raw["scheduling"]["testing"]["servers"][0] = "missing"
    write_yaml(config, raw)

    with pytest.raises(ValueError, match="must name configured remotes"):
        load_managed_project_config(config)


@pytest.mark.parametrize("field", ("python", "skill_root"))
def test_rejects_user_selected_controller_runtime_paths(
    tmp_path: Path,
    field: str,
) -> None:
    config = managed_config(tmp_path)
    raw = load_yaml(config)
    raw["controller"][field] = "/user/selected/path"
    write_yaml(config, raw)

    with pytest.raises(ValueError, match=rf"controller\.{field}.*no longer supported"):
        load_managed_project_config(config)


def test_controller_runtime_layout_is_derived_only_from_root() -> None:
    layout = controller_release_layout("/home/other-user/.remote-runner")

    assert layout.releases_root == "/home/other-user/.remote-runner/runner/releases"
    assert layout.current == "/home/other-user/.remote-runner/runner/current"
    assert layout.interpreter == (
        "/home/other-user/.remote-runner/runner/current/venv/bin/python"
    )


def test_rejects_legacy_fixed_workdir_config(tmp_path: Path) -> None:
    config = managed_config(tmp_path)
    value = load_managed_project_config(config)
    write_yaml(
        config,
        {
            "project_id": value.project_id,
            "controller": {"ssh": "controller_host", "root": "/controller"},
            "source": {"local_repo": "code"},
            "remote": {
                "compute-a": {
                    "workdir": "/srv/example/code",
                    "python": "/opt/example/bin/python3",
                }
            },
        },
    )

    with pytest.raises(ValueError, match="workdir is no longer supported"):
        load_managed_project_config(config)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("controller", {}, "controller.ssh"),
        ("source", {"local_repo": "code", "mode": "predeployed"}, "git-worktree"),
        ("scheduling", {"strategy": "normalized_load"}, "max_available_cores"),
    ],
)
def test_rejects_invalid_managed_contract(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    config = managed_config(tmp_path)
    raw = load_yaml(config)
    raw[field] = value
    write_yaml(config, raw)

    with pytest.raises(ValueError, match=message):
        load_managed_project_config(config)


@pytest.mark.parametrize("value", ("relative/output", "$HOME/output", "/srv//output"))
def test_rejects_invalid_remote_output_root(tmp_path: Path, value: str) -> None:
    config = managed_config(tmp_path)
    raw = load_yaml(config)
    raw["remote"]["compute-a"]["output_root"] = value
    write_yaml(config, raw)

    with pytest.raises(ValueError, match="output_root"):
        load_managed_project_config(config)
