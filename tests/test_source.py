from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from remote_runner._internal.source import (
    DeploymentTarget,
    prepare_revision,
    resolve_clean_head,
    select_historical_source_repo,
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


def test_historical_source_uses_clean_registered_worktree_when_configured_is_dirty(
    tmp_path: Path,
) -> None:
    source, revision = make_source(tmp_path)
    linked = tmp_path / "clean-linked"
    run("git", "worktree", "add", "--detach", str(linked), revision, cwd=source)
    dirty = source / "paper-plot.txt"
    dirty.write_text("local paper change\n", encoding="utf-8")
    status_before = run(
        "git", "status", "--porcelain", "--untracked-files=normal", cwd=source
    )

    selected = select_historical_source_repo(
        source,
        None,
        revisions=(revision,),
    )

    assert selected.source_repo == linked.resolve()
    assert selected.selection == "linked-worktree"
    assert selected.verified_revisions == (revision,)
    assert selected.audit() == {
        "selection": "linked-worktree",
        "source_repo": str(linked.resolve()),
        "clean_head": revision,
        "verified_revisions": [revision],
    }
    assert dirty.read_text(encoding="utf-8") == "local paper change\n"
    assert (
        run("git", "status", "--porcelain", "--untracked-files=normal", cwd=source)
        == status_before
    )


def test_historical_source_fails_closed_without_clean_linked_worktree(
    tmp_path: Path,
) -> None:
    source, revision = make_source(tmp_path)
    (source / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no trusted clean linked worktree"):
        select_historical_source_repo(source, None, revisions=(revision,))


def test_historical_source_requires_every_requested_revision(tmp_path: Path) -> None:
    source, revision = make_source(tmp_path)
    linked = tmp_path / "clean-linked"
    run("git", "worktree", "add", "--detach", str(linked), revision, cwd=source)
    (source / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="queued historical revision is unavailable"):
        select_historical_source_repo(
            source,
            None,
            revisions=(revision, "f" * 40),
        )


def test_explicit_historical_source_never_falls_back_from_dirty_override(
    tmp_path: Path,
) -> None:
    source, revision = make_source(tmp_path)
    linked = tmp_path / "clean-linked"
    run("git", "worktree", "add", "--detach", str(linked), revision, cwd=source)
    (source / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be clean"):
        select_historical_source_repo(source, source, revisions=(revision,))


def test_explicit_clean_historical_source_takes_priority(tmp_path: Path) -> None:
    source, revision = make_source(tmp_path)
    linked = tmp_path / "clean-linked"
    run("git", "worktree", "add", "--detach", str(linked), revision, cwd=source)
    (source / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    selected = select_historical_source_repo(
        source,
        linked,
        revisions=(revision,),
    )

    assert selected.source_repo == linked.resolve()
    assert selected.selection == "explicit"


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


def test_remote_preparation_uses_managed_ssh_for_push_and_verification(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    revision = "a" * 40
    ref = f"refs/remote-runner/example/{revision}"
    calls: list[list[str]] = []

    def runner(
        args: list[str], *, cwd: Path | None = None, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "--show-toplevel" in args:
            stdout = str(source)
        elif "HEAD^{commit}" in args:
            stdout = revision
        elif "status" in args:
            stdout = ""
        elif "ls-remote" in args:
            stdout = f"{revision}\t{ref}"
        else:
            stdout = ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    result = prepare_revision(
        source,
        project_id="example",
        targets=[DeploymentTarget("compute-a", "compute-a:/srv/repo.git")],
        timeout=17,
        runner=runner,
    )

    network_calls = [args for args in calls if "push" in args or "ls-remote" in args]
    assert len(network_calls) == 2
    for args in network_calls:
        assert args[:2] == ["git", "-c"]
        ssh_command = args[2]
        assert ssh_command.startswith("core.sshCommand=ssh ")
        assert "BatchMode=yes" in ssh_command
        assert "ConnectTimeout=17" in ssh_command
        assert "ControlMaster=auto" in ssh_command
        assert "ControlPersist=60" in ssh_command
        assert "ControlPath=~/.ssh/remote-runner-%C" in ssh_command
    assert result.prepared_servers == ("compute-a",)
