from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from remote_runner._internal.source import (
    DeploymentTarget,
    prepare_revision,
    resolve_clean_head,
)


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        list(args),
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def make_source(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    run("git", "init", "-q", str(source))
    run("git", "config", "user.name", "Test User", cwd=source)
    run("git", "config", "user.email", "test@example.com", cwd=source)
    (source / "experiment.py").write_text("print('ok')\n", encoding="utf-8")
    run("git", "add", "experiment.py", cwd=source)
    run("git", "commit", "-q", "-m", "initial", cwd=source)
    return source, run("git", "rev-parse", "HEAD", cwd=source)


def make_bare(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / name
    run("git", "init", "-q", "--bare", str(bare))
    return bare


def test_resolve_clean_head_rejects_dirty_source(tmp_path: Path) -> None:
    source, _revision = make_source(tmp_path)
    (source / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be clean"):
        resolve_clean_head(source)


def test_fanout_prepares_all_reachable_targets(tmp_path: Path) -> None:
    source, revision = make_source(tmp_path)
    first = make_bare(tmp_path, "first.git")
    second = make_bare(tmp_path, "second.git")

    result = prepare_revision(
        source,
        project_id="example",
        targets=[
            DeploymentTarget("compute-a", str(first)),
            DeploymentTarget("compute-b", str(second)),
        ],
    )

    assert result.revision == revision
    assert result.prepared_servers == ("compute-a", "compute-b")
    assert result.failures == ()
    for bare in (first, second):
        assert run("git", "--git-dir", str(bare), "rev-parse", result.ref) == revision


def test_prepares_queued_historical_revision_from_clean_local_repo(tmp_path: Path) -> None:
    source, historical_revision = make_source(tmp_path)
    (source / "experiment.py").write_text("print('new')\n", encoding="utf-8")
    run("git", "add", "experiment.py", cwd=source)
    run("git", "commit", "-q", "-m", "new head", cwd=source)
    assert run("git", "rev-parse", "HEAD", cwd=source) != historical_revision
    target = make_bare(tmp_path, "new-server.git")

    result = prepare_revision(
        source,
        project_id="example",
        targets=[DeploymentTarget("archive", str(target))],
        explicit_server="archive",
        revision=historical_revision,
    )

    assert result.revision == historical_revision
    assert run("git", "--git-dir", str(target), "rev-parse", result.ref) == historical_revision


def test_automatic_fanout_records_partial_success(tmp_path: Path) -> None:
    source, _revision = make_source(tmp_path)
    reachable = make_bare(tmp_path, "reachable.git")

    result = prepare_revision(
        source,
        project_id="example",
        targets=[
            DeploymentTarget("compute-a", str(reachable)),
            DeploymentTarget("compute-b", str(tmp_path / "missing.git")),
        ],
    )

    assert result.prepared_servers == ("compute-a",)
    assert [item.name for item in result.failures] == ["compute-b"]


def test_explicit_server_failure_never_falls_back(tmp_path: Path) -> None:
    source, _revision = make_source(tmp_path)

    with pytest.raises(RuntimeError, match="explicit server archive"):
        prepare_revision(
            source,
            project_id="example",
            targets=[DeploymentTarget("archive", str(tmp_path / "missing.git"))],
            explicit_server="archive",
        )
