from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from remote_runner._internal import submission
from remote_runner._internal.config import load_managed_project_config
from remote_runner._internal.execution_registry import load_yaml, write_yaml
from remote_runner._internal.preparation_manifest import (
    build_preparation_manifest,
    write_preparation_manifest,
)
from remote_runner._internal.source import PreparationResult, PreparedServer


def config(tmp_path: Path) -> Path:
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
                    "output_root": "/srv/output",
                }
            },
        },
    )
    return path


def binding_template() -> dict[str, object]:
    return {
        "kind": "run_binding",
        "schema_version": 1,
        "targets": [
            {
                "study_id": "study-0123456789abcdef",
                "origin_design_revision_id": "design-0123456789abcdef",
                "plan_digest": "sha256:" + "1" * 64,
                "point_id": "point-0123456789abcdef",
                "point_revision_id": "pointrev-0123456789abcdef",
                "point_revision_digest": "sha256:" + "2" * 64,
                "setting_digest": "sha256:" + "3" * 64,
                "result_group_id": "primary",
                "contribution_role": "primary",
            }
        ],
        "expects_result_manifest": False,
        "metadata": {},
    }


def test_experiment_binding_template_is_finalized_for_exact_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "binding.json"
    path.write_text(
        json.dumps(binding_template()),
        encoding="utf-8",
    )

    binding = submission._finalize_experiment_binding(
        path,
        run_id="rr-0123456789abcdef",
        source_revision="a" * 40,
    )

    assert binding is not None
    assert binding["binding_id"].startswith("binding-")
    assert binding["run_id"] == "rr-0123456789abcdef"
    assert binding["source_revision"] == "a" * 40
    assert binding["binding_digest"].startswith("sha256:")


def test_supplied_binding_identity_is_reproducible_and_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "binding.json"
    template = binding_template()
    template["binding_id"] = "binding-0123456789abcdef"
    path.write_text(json.dumps(template), encoding="utf-8")

    first = submission._finalize_experiment_binding(
        path,
        run_id="rr-0123456789abcdef",
        source_revision="a" * 40,
    )
    second = submission._finalize_experiment_binding(
        path,
        run_id="rr-0123456789abcdef",
        source_revision="a" * 40,
    )

    assert first == second
    assert first is not None
    assert first["binding_id"] == "binding-0123456789abcdef"

    frozen = dict(first)
    path.write_text(json.dumps(frozen), encoding="utf-8")
    assert (
        submission._finalize_experiment_binding(
            path,
            run_id="rr-0123456789abcdef",
            source_revision="a" * 40,
        )
        == first
    )

    frozen_targets = list(frozen["targets"])
    frozen_targets[0] = {**frozen_targets[0], "setting_digest": "sha256:" + "4" * 64}
    frozen["targets"] = frozen_targets
    path.write_text(json.dumps(frozen), encoding="utf-8")
    with pytest.raises(ValueError, match="binding_digest does not match"):
        submission._finalize_experiment_binding(
            path,
            run_id="rr-0123456789abcdef",
            source_revision="a" * 40,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("run_id", "rr-fedcba9876543210", "run_id does not match"),
        ("source_revision", "b" * 40, "source_revision does not match"),
    ],
)
def test_binding_template_rejects_supplied_run_identity_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    path = tmp_path / "binding.json"
    template = binding_template()
    template[field] = value
    path.write_text(json.dumps(template), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        submission._finalize_experiment_binding(
            path,
            run_id="rr-0123456789abcdef",
            source_revision="a" * 40,
        )


@pytest.mark.parametrize("override_name", [None, "task-repo"])
def test_managed_run_fans_out_selected_source_and_submits_prepared_manifest(
    tmp_path: Path,
    monkeypatch,
    override_name: str | None,
) -> None:
    config_path = config(tmp_path)
    (tmp_path / "code").mkdir()
    source_override = tmp_path / override_name if override_name is not None else None
    if source_override is not None:
        source_override.mkdir()
    pool = [
        {
            "name": "compute-a",
            "ssh": "compute-a",
            "ssh_profile": "intranet",
            "cores": 256,
            "priority": 100,
            "probe": {"reachable": True},
            "runtime": {
                "bare_repo": "/srv/repo.git",
                "worktree_root": "/srv/worktrees",
                "python": "/opt/python3",
                "output_root": "/srv/output",
            },
        }
    ]
    monkeypatch.setattr(
        submission, "probe_project_pool", lambda *_args, **_kwargs: pool
    )
    prepared_from: list[Path] = []

    def prepare(local_repo: Path, **_kwargs) -> PreparationResult:
        prepared_from.append(local_repo)
        return PreparationResult(
            revision="a" * 40,
            ref="refs/remote-runner/example/" + "a" * 40,
            prepared=(
                PreparedServer("compute-a", "compute-a:/srv/repo.git", "ref", "a" * 40),
            ),
            failures=(),
        )

    monkeypatch.setattr(submission, "prepare_revision", prepare)
    submitted: dict[str, object] = {}
    monkeypatch.setattr(
        submission,
        "call_controller",
        lambda _config, _action, *, timeout, payload: (
            submitted.update(payload) or {"outcome": {"action": "queued"}}
        ),
    )
    args = argparse.Namespace(
        project_config=config_path,
        source_repo=source_override,
        server_registry=tmp_path / "servers.yaml",
        server=None,
        ssh_profile="auto",
        label="test",
        task_id="task-1",
        result_intent="supporting",
        result_tags=["purpose=validation", "campaign=backfill"],
        queue_priority="urgent",
        minimum_cores=128,
        command="python experiment.py",
        output_path=None,
        output_relpath="validation/result.json",
        output_metadata=None,
        privacy=None,
        run_id="rr-0123456789abcdef",
        timeout=8,
        prepare_timeout=60,
    )

    result = submission.submit(args)

    assert prepared_from == [(source_override or (tmp_path / "code")).resolve()]
    assert result["prepared_servers"] == ["compute-a"]
    assert submitted["revision"] == "a" * 40
    assert submitted["result_intent"] == "supporting"
    assert submitted["result_tags"] == {
        "campaign": "backfill",
        "purpose": "validation",
    }
    assert submitted["queue_priority"] == "urgent"
    assert submitted["minimum_cores"] == 128
    assert result["minimum_cores"] == 128
    assert result["result_intent"] == "supporting"
    assert result["result_tags"] == {
        "campaign": "backfill",
        "purpose": "validation",
    }
    assert submitted["worker_arg"] == "--num-workers"
    assert submitted["worker_policy"] == "auto"
    assert submitted["output_relpath"] == "validation/result.json"
    assert submitted["output_path"] is None
    prepared = submitted["prepared_servers"]
    assert isinstance(prepared, list)
    assert prepared[0]["configured_cores"] == 256


def test_relative_source_override_fails_before_pool_probe(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = config(tmp_path)

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("pool probe must not run for an invalid source override")

    monkeypatch.setattr(submission, "probe_project_pool", unexpected_probe)

    with pytest.raises(ValueError, match="--source-repo must be an absolute path"):
        submission.submit(
            argparse.Namespace(
                project_config=config_path,
                source_repo=Path("relative-repo"),
            )
        )


def test_all_server_uses_automatic_pool_for_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = config(tmp_path)
    (tmp_path / "code").mkdir()
    selected: dict[str, object] = {}
    pool = [
        {
            "name": "compute-a",
            "ssh": "compute-a",
            "ssh_profile": "intranet",
            "cores": 256,
            "priority": 100,
            "probe": {"reachable": True},
            "runtime": {
                "bare_repo": "/srv/repo.git",
                "worktree_root": "/srv/worktrees",
                "python": "/opt/python3",
                "output_root": "/srv/output",
            },
        }
    ]

    def probe(*_args, **kwargs):
        selected["pool_server"] = kwargs["explicit_server"]
        return pool

    def prepare(*_args, **kwargs):
        selected["prepare_server"] = kwargs["explicit_server"]
        return PreparationResult(
            revision="a" * 40,
            ref="refs/remote-runner/example/" + "a" * 40,
            prepared=(
                PreparedServer("compute-a", "compute-a:/srv/repo.git", "ref", "a" * 40),
            ),
            failures=(),
        )

    monkeypatch.setattr(submission, "probe_project_pool", probe)
    monkeypatch.setattr(submission, "prepare_revision", prepare)
    submitted: dict[str, object] = {}
    monkeypatch.setattr(
        submission,
        "call_controller",
        lambda _config, _action, *, timeout, payload: (
            submitted.update(payload) or {"outcome": {"action": "queued"}}
        ),
    )

    result = submission.submit(
        argparse.Namespace(
            project_config=config_path,
            source_repo=None,
            server_registry=tmp_path / "servers.yaml",
            server="all",
            candidate_servers=None,
            ssh_profile="auto",
            workload_class="standard",
            label="automatic",
            task_id="task-1",
            queue_priority="normal",
            minimum_cores=1,
            command="python experiment.py",
            output_path=None,
            output_relpath=None,
            output_metadata=None,
            privacy=None,
            run_id="rr-0123456789abcdef",
            timeout=8,
            prepare_timeout=60,
            prepared_manifest=None,
        )
    )

    assert selected == {"pool_server": None, "prepare_server": None}
    assert result["prepared_servers"] == ["compute-a"]
    assert result["server_scope"] == "all"
    assert submitted["server_scope"] == "all"


def test_test_workload_allows_configured_testing_pool_subset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = config(tmp_path)
    raw = load_yaml(config_path)
    raw["remote"]["compute-d"] = {
        "bare_repo": "/srv/compute-d/repo.git",
        "worktree_root": "/srv/compute-d/worktrees",
        "python": "/opt/compute-d/python3",
        "output_root": "/srv/compute-d/output",
    }
    raw["scheduling"] = {"testing": {"servers": ["compute-a", "compute-d"]}}
    write_yaml(config_path, raw)
    (tmp_path / "code").mkdir()
    selected: dict[str, object] = {}
    pool = [
        {
            "name": "compute-d",
            "ssh": "server-compute-d",
            "ssh_profile": "intranet",
            "cores": 256,
            "priority": 100,
            "test_slots": 1,
            "probe": {"reachable": True},
            "runtime": {
                "bare_repo": "/srv/compute-d/repo.git",
                "worktree_root": "/srv/compute-d/worktrees",
                "python": "/opt/compute-d/python3",
                "output_root": "/srv/compute-d/output",
            },
        }
    ]

    def probe(*_args, **kwargs):
        selected["pool_server"] = kwargs["explicit_server"]
        selected["candidate_servers"] = kwargs["candidate_servers"]
        return pool

    def prepare(*_args, **kwargs):
        selected["prepare_server"] = kwargs["explicit_server"]
        return PreparationResult(
            revision="a" * 40,
            ref="refs/remote-runner/example/" + "a" * 40,
            prepared=(
                PreparedServer(
                    "compute-d",
                    "server-compute-d:/srv/compute-d/repo.git",
                    "ref",
                    "a" * 40,
                ),
            ),
            failures=(),
        )

    monkeypatch.setattr(submission, "probe_project_pool", probe)
    monkeypatch.setattr(submission, "prepare_revision", prepare)
    submitted: dict[str, object] = {}
    monkeypatch.setattr(
        submission,
        "call_controller",
        lambda _config, _action, *, timeout, payload: (
            submitted.update(payload) or {"outcome": {"action": "queued"}}
        ),
    )

    result = submission.submit(
        argparse.Namespace(
            project_config=config_path,
            source_repo=None,
            server_registry=tmp_path / "servers.yaml",
            server=None,
            candidate_servers=["compute-d"],
            ssh_profile="auto",
            workload_class="test",
            label="pytest",
            task_id="task-1",
            queue_priority="normal",
            minimum_cores=1,
            command="python -m pytest -q",
            output_path=None,
            output_relpath=None,
            output_metadata=None,
            privacy=None,
            run_id="rr-0123456789abcdef",
            timeout=8,
            prepare_timeout=60,
            prepared_manifest=None,
        )
    )

    assert selected == {
        "pool_server": None,
        "candidate_servers": ("compute-d",),
        "prepare_server": None,
    }
    assert result["prepared_servers"] == ["compute-d"]
    assert result["workload_class"] == "test"
    assert submitted["workload_class"] == "test"
    assert submitted["worker_policy"] == "exact"
    assert submitted["prepared_servers"][0]["test_slots"] == 1


def test_test_workload_rejects_candidate_outside_testing_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = config(tmp_path)
    raw = load_yaml(config_path)
    raw["remote"]["compute-d"] = {
        "bare_repo": "/srv/compute-d/repo.git",
        "worktree_root": "/srv/compute-d/worktrees",
        "python": "/opt/compute-d/python3",
    }
    raw["scheduling"] = {"testing": {"servers": ["compute-a"]}}
    write_yaml(config_path, raw)
    (tmp_path / "code").mkdir()

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("pool probe must not run for an invalid testing subset")

    monkeypatch.setattr(submission, "probe_project_pool", unexpected_probe)

    with pytest.raises(
        ValueError,
        match=r"outside testing pool: compute-d",
    ):
        submission.submit(
            argparse.Namespace(
                project_config=config_path,
                source_repo=None,
                output_relpath=None,
                output_path=None,
                minimum_cores=1,
                workload_class="test",
                server=None,
                candidate_servers=["compute-d"],
            )
        )


def test_test_workload_filters_reused_preparation_to_testing_subset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = config(tmp_path)
    raw = load_yaml(config_path)
    raw["remote"]["compute-d"] = {
        "bare_repo": "/srv/compute-d/repo.git",
        "worktree_root": "/srv/compute-d/worktrees",
        "python": "/opt/compute-d/python3",
    }
    raw["scheduling"] = {"testing": {"servers": ["compute-a", "compute-d"]}}
    write_yaml(config_path, raw)
    (tmp_path / "code").mkdir()
    prepared_servers = [
        {
            "name": name,
            "configured_cores": 256,
            "test_slots": 1,
        }
        for name in ("compute-a", "compute-d")
    ]
    monkeypatch.setattr(
        submission,
        "load_preparation_manifest",
        lambda *_args, **_kwargs: {
            "revision": "a" * 40,
            "prepared_servers": prepared_servers,
            "preparation_failures": [],
        },
    )
    submitted: dict[str, object] = {}
    monkeypatch.setattr(
        submission,
        "call_controller",
        lambda _config, _action, *, timeout, payload: (
            submitted.update(payload) or {"outcome": {"action": "queued"}}
        ),
    )

    result = submission.submit(
        argparse.Namespace(
            project_config=config_path,
            source_repo=None,
            server_registry=tmp_path / "servers.yaml",
            server=None,
            candidate_servers=["compute-d"],
            ssh_profile="auto",
            workload_class="test",
            label="pytest",
            task_id="task-1",
            queue_priority="normal",
            minimum_cores=1,
            command="python -m pytest -q",
            output_path=None,
            output_relpath=None,
            output_metadata=None,
            privacy=None,
            run_id="rr-0123456789abcdef",
            timeout=8,
            prepare_timeout=60,
            prepared_manifest=tmp_path / "prepared.json",
        )
    )

    assert result["prepared_servers"] == ["compute-d"]
    assert [server["name"] for server in submitted["prepared_servers"]] == ["compute-d"]


def test_managed_run_reuses_preparation_without_remote_probe_or_push(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = config(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=source, check=True
    )
    (source / "experiment.py").write_text("print('ok')\n", encoding="utf-8")
    subprocess.run(["git", "add", "experiment.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=source, check=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    registry = tmp_path / "servers.yaml"
    registry.write_text("servers: {}\n", encoding="utf-8")
    managed_config = load_managed_project_config(config_path)
    preparation = PreparationResult(
        revision=revision,
        ref=f"refs/remote-runner/example/{revision}",
        prepared=(
            PreparedServer("compute-a", "compute-a:/srv/repo.git", "ref", revision),
        ),
        failures=(),
    )
    prepared_servers = [
        {
            "name": "compute-a",
            "ssh": "compute-a",
            "ssh_profile": "intranet",
            "configured_cores": 256,
            "priority": 100,
            "bare_repo": "/srv/repo.git",
            "worktree_root": "/srv/worktrees",
            "python": "/opt/python3",
            "output_root": "/srv/output",
        }
    ]
    manifest_path = tmp_path / "prepared.json"
    write_preparation_manifest(
        manifest_path,
        build_preparation_manifest(
            config=managed_config,
            server_registry_path=registry,
            preparation=preparation,
            prepared_servers=prepared_servers,
        ),
    )

    monkeypatch.setattr(
        submission,
        "probe_project_pool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reused preparation must not probe servers")
        ),
    )
    monkeypatch.setattr(
        submission,
        "prepare_revision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reused preparation must not push the revision")
        ),
    )
    submitted: dict[str, object] = {}
    monkeypatch.setattr(
        submission,
        "call_controller",
        lambda _config, _action, *, timeout, payload: (
            submitted.update(payload) or {"outcome": {"action": "queued"}}
        ),
    )
    args = argparse.Namespace(
        project_config=config_path,
        source_repo=source,
        server_registry=registry,
        server=None,
        ssh_profile="auto",
        label="reused",
        task_id="task-1",
        queue_priority="normal",
        minimum_cores=128,
        command="python experiment.py",
        output_path=None,
        output_relpath=None,
        output_metadata=None,
        privacy=None,
        run_id="rr-0123456789abcdef",
        timeout=8,
        prepare_timeout=60,
        prepared_manifest=manifest_path,
    )

    result = submission.submit(args)

    assert result["preparation_reused"] is True
    assert result["revision"] == revision
    assert result["prepared_servers"] == ["compute-a"]
    assert submitted["prepared_servers"] == [{**prepared_servers[0], "test_slots": 0}]
    assert submitted["minimum_cores"] == 128


def test_minimum_cores_filters_reused_prepared_candidates() -> None:
    prepared = [
        {"name": "compute-a", "configured_cores": 256},
        {"name": "archive", "configured_cores": 32},
    ]

    assert submission._eligible_prepared_servers(
        prepared,
        minimum_cores=256,
    ) == [{**prepared[0], "output_root": None, "test_slots": 0}]


def test_minimum_cores_rejects_empty_prepared_candidate_set() -> None:
    with pytest.raises(ValueError, match="no prepared server has at least 256"):
        submission._eligible_prepared_servers(
            [{"name": "archive", "configured_cores": 32}],
            minimum_cores=256,
        )


def test_candidate_allow_list_filters_reused_prepared_candidates() -> None:
    prepared = [
        {"name": "compute-b", "configured_cores": 128},
        {"name": "compute-a", "configured_cores": 256},
        {"name": "compute-c", "configured_cores": 32},
        {"name": "CPU128", "configured_cores": 128},
    ]

    assert [
        server["name"]
        for server in submission._eligible_prepared_servers(
            prepared,
            minimum_cores=1,
            candidate_servers=("compute-b", "compute-a", "compute-c"),
        )
    ] == ["compute-b", "compute-a", "compute-c"]


def test_candidate_allow_list_rejects_empty_prepared_intersection() -> None:
    with pytest.raises(ValueError, match="no allowed candidate server"):
        submission._eligible_prepared_servers(
            [{"name": "CPU128", "configured_cores": 128}],
            minimum_cores=1,
            candidate_servers=("compute-b", "compute-a", "compute-c"),
        )


def test_candidate_allow_list_rejects_non_default_minimum_cores(
    tmp_path: Path,
) -> None:
    config_path = config(tmp_path)
    (tmp_path / "code").mkdir()

    with pytest.raises(
        ValueError,
        match="--candidate-server cannot be combined with a non-default --min-cores",
    ):
        submission.submit(
            argparse.Namespace(
                project_config=config_path,
                source_repo=None,
                output_relpath=None,
                output_path=None,
                minimum_cores=128,
                workload_class="standard",
                server=None,
                candidate_servers=["compute-a"],
            )
        )


def test_reused_preparation_rejects_server_registry_drift(tmp_path: Path) -> None:
    config_path = config(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=source, check=True
    )
    (source / "experiment.py").write_text("print('ok')\n", encoding="utf-8")
    subprocess.run(["git", "add", "experiment.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=source, check=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    registry = tmp_path / "servers.yaml"
    registry.write_text("servers: {}\n", encoding="utf-8")
    managed_config = load_managed_project_config(config_path)
    preparation = PreparationResult(
        revision=revision,
        ref=f"refs/remote-runner/example/{revision}",
        prepared=(
            PreparedServer("compute-a", "compute-a:/srv/repo.git", "ref", revision),
        ),
        failures=(),
    )
    manifest_path = tmp_path / "prepared.json"
    write_preparation_manifest(
        manifest_path,
        build_preparation_manifest(
            config=managed_config,
            server_registry_path=registry,
            preparation=preparation,
            prepared_servers=[
                {
                    "name": "compute-a",
                    "ssh": "compute-a",
                    "ssh_profile": "intranet",
                    "configured_cores": 256,
                    "priority": 100,
                    "bare_repo": "/srv/repo.git",
                    "worktree_root": "/srv/worktrees",
                    "python": "/opt/python3",
                    "output_root": "/srv/output",
                }
            ],
        ),
    )
    registry.write_text("servers:\n  changed: {}\n", encoding="utf-8")
    args = argparse.Namespace(
        project_config=config_path,
        source_repo=source,
        server_registry=registry,
        server=None,
        prepared_manifest=manifest_path,
    )

    with pytest.raises(ValueError, match="server registry changed"):
        submission.submit(args)


@pytest.mark.parametrize(
    "value",
    ("../result.json", "/absolute/result.json", "$HOME/result.json"),
)
def test_invalid_output_relpath_fails_before_pool_probe(
    tmp_path: Path,
    monkeypatch,
    value: str,
) -> None:
    config_path = config(tmp_path)

    monkeypatch.setattr(
        submission,
        "probe_project_pool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid output must fail before pool probing")
        ),
    )

    with pytest.raises(ValueError):
        submission.submit(
            argparse.Namespace(
                project_config=config_path,
                source_repo=None,
                output_relpath=value,
                output_path=None,
            )
        )


def test_relative_output_requires_roots_for_every_eligible_server() -> None:
    with pytest.raises(ValueError, match="missing: archive"):
        submission._validate_output_candidates(
            [
                {"name": "compute-a", "output_root": "/home/a/project"},
                {"name": "archive", "output_root": None},
            ],
            output_relpath="validation/result.json",
        )
