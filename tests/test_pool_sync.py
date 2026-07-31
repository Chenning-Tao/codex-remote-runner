from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

from remote_runner._internal import pool_sync
from remote_runner._internal.execution_registry import write_yaml
from remote_runner._internal.source import (
    HistoricalSourceSelection,
    PreparationResult,
    PreparedServer,
)


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
    source_repo = (tmp_path / "code").resolve()
    monkeypatch.setattr(
        pool_sync,
        "select_historical_source_repo",
        lambda *_args, **_kwargs: HistoricalSourceSelection(
            source_repo=source_repo,
            selection="configured",
            clean_head=revision,
            verified_revisions=(revision,),
        ),
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

    assert prepared == [(revision, "archive")]
    assert result["pending_count"] == 2
    assert result["update_count"] == 2
    assert result["source"] == {
        "selection": "configured",
        "source_repo": str(source_repo),
        "clean_head": revision,
        "verified_revisions": [revision],
    }
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


def test_sync_pool_uses_one_clean_linked_source_for_all_historical_revisions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def git(*args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    config = project_config(tmp_path)
    source = tmp_path / "code"
    git("init", "-q", str(source))
    git("config", "user.name", "Test User", cwd=source)
    git("config", "user.email", "test@example.com", cwd=source)
    experiment = source / "experiment.py"
    experiment.write_text("print('first')\n", encoding="utf-8")
    git("add", "experiment.py", cwd=source)
    git("commit", "-q", "-m", "first", cwd=source)
    first_revision = git("rev-parse", "HEAD", cwd=source)
    experiment.write_text("print('second')\n", encoding="utf-8")
    git("add", "experiment.py", cwd=source)
    git("commit", "-q", "-m", "second", cwd=source)
    second_revision = git("rev-parse", "HEAD", cwd=source)
    clean_source = tmp_path / "clean-source"
    git(
        "worktree",
        "add",
        "--detach",
        str(clean_source),
        second_revision,
        cwd=source,
    )
    dirty_file = source / "paper-plot.txt"
    dirty_file.write_text("local plot change\n", encoding="utf-8")
    status_before = git(
        "status", "--porcelain", "--untracked-files=normal", cwd=source
    )
    pending = [
        {
            "run_id": run_id,
            "revision": revision,
            "minimum_cores": 1,
            "workload_class": "standard",
            "prepared_servers": ["compute-a"],
            "output_relpath": None,
        }
        for run_id, revision in (
            ("rr-0123456789abcdef", first_revision),
            ("rr-fedcba9876543210", second_revision),
        )
    ]

    def controller(_config, action, *, payload=None, **_kwargs):
        if action == "pending-all":
            return {"jobs": pending, "count": len(pending)}
        assert action == "extend-all"
        assert payload is not None
        return {
            "results": [],
            "extended_count": len(payload["updates"]),
            "dispatcher_started": False,
        }

    prepared: list[tuple[Path, str]] = []

    def prepare(source_repo: Path, *, revision: str, targets, **_kwargs):
        prepared.append((source_repo, revision))
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

    assert prepared == [
        (clean_source.resolve(), first_revision),
        (clean_source.resolve(), second_revision),
    ]
    assert result["source"] == {
        "selection": "linked-worktree",
        "source_repo": str(clean_source.resolve()),
        "clean_head": second_revision,
        "verified_revisions": [first_revision, second_revision],
    }
    assert dirty_file.read_text(encoding="utf-8") == "local plot change\n"
    assert (
        git("status", "--porcelain", "--untracked-files=normal", cwd=source)
        == status_before
    )
