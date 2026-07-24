from __future__ import annotations

import argparse
from pathlib import Path

from remote_runner._internal import pool_sync
from remote_runner._internal.execution_registry import write_yaml
from remote_runner._internal.source import PreparationResult, PreparedServer


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
                for name in ("compute-a", "archive")
            },
        },
    )
    (tmp_path / "code").mkdir()
    return path


def candidate(name: str) -> dict[str, object]:
    return {
        "name": name,
        "ssh": name.lower(),
        "ssh_profile": "intranet",
        "cores": 256 if name == "compute-a" else 128,
        "priority": 10,
        "test_slots": 0,
        "probe": {"reachable": True},
        "runtime": {
            "bare_repo": f"/srv/{name}/repo.git",
            "worktree_root": f"/srv/{name}/worktrees",
            "python": f"/opt/{name}/python3",
            "output_root": f"/srv/{name}/output",
        },
    }


def test_sync_pool_prepares_new_server_once_for_shared_queued_revision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = project_config(tmp_path)
    revision = "a" * 40
    pending = [
        {
            "run_id": run_id,
            "revision": revision,
            "minimum_cores": 1,
            "workload_class": "standard",
            "prepared_servers": ["compute-a"],
            "output_relpath": "results/output.json",
        }
        for run_id in ("rr-0123456789abcdef", "rr-fedcba9876543210")
    ]
    controller_calls: list[tuple[str, object]] = []

    def controller(_config, action, *, timeout, payload=None):
        controller_calls.append((action, payload))
        if action == "pending-all":
            return {"jobs": pending, "count": 2}
        return {"results": [], "extended_count": 2, "dispatcher_started": True}

    prepared: list[tuple[str, str]] = []

    def prepare(_source, *, revision, targets, **_kwargs):
        prepared.append((revision, targets[0].name))
        target = targets[0]
        return PreparationResult(
            revision=revision,
            ref=f"refs/remote-runner/example/{revision}",
            prepared=(
                PreparedServer(
                    target.name,
                    target.remote_url,
                    f"refs/remote-runner/example/{revision}",
                    revision,
                ),
            ),
            failures=(),
        )

    monkeypatch.setattr(pool_sync, "call_controller", controller)
    monkeypatch.setattr(
        pool_sync,
        "probe_project_pool",
        lambda *_args, **_kwargs: [candidate("compute-a"), candidate("archive")],
    )
    monkeypatch.setattr(pool_sync, "prepare_revision", prepare)

    result = pool_sync.sync(
        argparse.Namespace(
            project_config=config,
            source_repo=None,
            server_registry=tmp_path / "servers.yaml",
            ssh_profile="auto",
            timeout=8,
            prepare_timeout=60,
        )
    )

    assert prepared == [(revision, "archive")]
    assert result["pending_count"] == 2
    assert result["update_count"] == 2
    updates = controller_calls[1][1]["updates"]
    assert [update["run_id"] for update in updates] == [
        "rr-0123456789abcdef",
        "rr-fedcba9876543210",
    ]
    assert all(update["prepared_servers"][0]["name"] == "archive" for update in updates)


def test_sync_pool_with_no_pending_jobs_does_not_probe(tmp_path: Path, monkeypatch) -> None:
    config = project_config(tmp_path)
    monkeypatch.setattr(
        pool_sync,
        "call_controller",
        lambda *_args, **_kwargs: {"jobs": [], "count": 0},
    )
    monkeypatch.setattr(
        pool_sync,
        "probe_project_pool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not probe")),
    )

    result = pool_sync.sync(
        argparse.Namespace(
            project_config=config,
            source_repo=None,
            server_registry=tmp_path / "servers.yaml",
            ssh_profile="auto",
            timeout=8,
            prepare_timeout=60,
        )
    )

    assert result["pending_count"] == 0
    assert result["update_count"] == 0
