from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from remote_runner._internal import server_addition
from remote_runner._internal.execution_registry import write_yaml
from remote_runner._internal.source import (
    HistoricalSourceSelection,
    PreparationResult,
    PreparedServer,
)


RUN_ID = "rr-0123456789abcdef"
REVISION = "a" * 40


def project_config(tmp_path: Path) -> Path:
    path = tmp_path / ".remote-runner.yaml"
    write_yaml(
        path,
        {
            "project_id": "example",
            "controller": {"ssh": "controller_host", "root": "/controller"},
            "source": {"local_repo": "code"},
            "remote": {
                name: {
                    "bare_repo": f"/srv/{name}/repo.git",
                    "worktree_root": f"/srv/{name}/worktrees",
                    "python": f"/opt/{name}/python3",
                    "output_root": f"/srv/{name}/output",
                }
                for name in ("compute-c", "compute-d")
            },
        },
    )
    (tmp_path / "code").mkdir()
    return path


def args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_config=project_config(tmp_path),
        source_repo=None,
        server_registry=tmp_path / "servers.yaml",
        run_id=RUN_ID,
        server="compute-d",
        ssh_profile="auto",
        timeout=8,
        prepare_timeout=60,
    )


def queued_job(*, output_path: str | None = None) -> dict[str, object]:
    return {
        "job": {
            "run_id": RUN_ID,
            "revision": REVISION,
            "minimum_cores": 1,
            "workload_class": "standard",
            "prepared_servers": ["compute-c"],
            "output_relpath": None,
            "output_path": output_path,
        }
    }


def candidate() -> dict[str, object]:
    return {
        "name": "compute-d",
        "ssh": "server-compute-d",
        "ssh_profile": "intranet",
        "cores": 128,
        "priority": 20,
        "test_slots": 0,
        "probe": {"reachable": True},
        "runtime": {
            "bare_repo": "/srv/compute-d/repo.git",
            "worktree_root": "/srv/compute-d/worktrees",
            "python": "/opt/compute-d/python3",
            "output_root": "/srv/compute-d/output",
        },
    }


def test_add_prepares_queued_revision_and_extends_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[str, ...], object]] = []

    def controller(_config, action, *, timeout, action_args=(), payload=None):
        calls.append((action, action_args, payload))
        if action == "queued-job":
            return queued_job()
        return {
            "run_id": RUN_ID,
            "status": "extended",
            "added_servers": 1,
            "prepared_servers": ["compute-c", "compute-d"],
            "dispatcher_started": False,
        }

    probed: dict[str, object] = {}

    def probe(_config, _registry, **kwargs):
        probed.update(kwargs)
        return [candidate()]

    prepared: dict[str, object] = {}

    def prepare(source: Path, **kwargs) -> PreparationResult:
        prepared.update(source=source, **kwargs)
        return PreparationResult(
            revision=REVISION,
            ref=f"refs/remote-runner/example/{REVISION}",
            prepared=(
                PreparedServer(
                    "compute-d",
                    "server-compute-d:/srv/compute-d/repo.git",
                    "ref",
                    REVISION,
                ),
            ),
            failures=(),
        )

    monkeypatch.setattr(server_addition, "call_controller", controller)
    monkeypatch.setattr(server_addition, "probe_project_pool", probe)
    monkeypatch.setattr(server_addition, "prepare_revision", prepare)

    addition_args = args(tmp_path)
    source_repo = (addition_args.project_config.parent / "code").resolve()
    monkeypatch.setattr(
        server_addition,
        "select_historical_source_repo",
        lambda *_args, **_kwargs: HistoricalSourceSelection(
            source_repo=source_repo,
            selection="configured",
            clean_head=REVISION,
            verified_revisions=(REVISION,),
        ),
    )
    addition_args.placement_token = "placement-token"
    result = server_addition.add(addition_args)

    assert probed["explicit_server"] == "compute-d"
    assert probed["minimum_cores"] == 1
    assert prepared["revision"] == REVISION
    assert prepared["explicit_server"] == "compute-d"
    assert [call[0] for call in calls] == ["queued-job", "extend-job"]
    assert calls[1][1] == ("--run-id", RUN_ID)
    payload = calls[1][2]
    assert isinstance(payload, dict)
    prepared_servers = payload["prepared_servers"]
    assert isinstance(prepared_servers, list)
    assert prepared_servers[0]["name"] == "compute-d"
    assert payload["placement_token"] == "placement-token"
    assert result["prepared_servers"] == ["compute-c", "compute-d"]
    assert result["source"] == {
        "selection": "configured",
        "source_repo": str(source_repo),
        "clean_head": REVISION,
        "verified_revisions": [REVISION],
    }
    assert result["outcome"] == {"action": "extended", "added_servers": 1}


def test_add_is_idempotent_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = queued_job()
    pending_job = pending["job"]
    assert isinstance(pending_job, dict)
    pending_job["prepared_servers"] = ["compute-c", "compute-d"]
    monkeypatch.setattr(
        server_addition,
        "call_controller",
        lambda *_args, **_kwargs: pending,
    )
    monkeypatch.setattr(
        server_addition,
        "probe_project_pool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not probe an already allowed server")
        ),
    )

    result = server_addition.add(args(tmp_path))

    assert result["outcome"] == {
        "action": "unchanged",
        "reason": "server already allowed",
    }


def test_add_rejects_nonportable_absolute_output_before_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server_addition,
        "call_controller",
        lambda *_args, **_kwargs: queued_job(output_path="/srv/compute-c/output.json"),
    )

    with pytest.raises(ValueError, match="historical queued run"):
        server_addition.add(args(tmp_path))


def test_add_rejects_test_server_outside_configured_test_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = queued_job()
    pending_job = pending["job"]
    assert isinstance(pending_job, dict)
    pending_job["workload_class"] = "test"
    monkeypatch.setattr(
        server_addition,
        "call_controller",
        lambda *_args, **_kwargs: pending,
    )

    with pytest.raises(ValueError, match="not in scheduling.testing.servers"):
        server_addition.add(args(tmp_path))
