from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from remote_runner._internal.worktree import build_program, parse_result


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        list(args),
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    bare = tmp_path / "repo.git"
    source.mkdir()
    run("git", "init", "-q", str(source))
    run("git", "config", "user.name", "Test User", cwd=source)
    run("git", "config", "user.email", "test@example.com", cwd=source)
    (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
    run("git", "add", "main.py", cwd=source)
    run("git", "commit", "-q", "-m", "initial", cwd=source)
    revision = run("git", "rev-parse", "HEAD", cwd=source)
    run("git", "init", "-q", "--bare", str(bare))
    run("git", "push", str(bare), f"{revision}:refs/remote-runner/example/{revision}", cwd=source)
    return source, bare, revision


def execute(program: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-"],
        input=program,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_creates_and_reuses_exact_clean_worktree(tmp_path: Path) -> None:
    _source, bare, revision = repository(tmp_path)
    root = tmp_path / "worktrees"
    program = build_program(
        bare_repo=str(bare),
        worktree_root=str(root),
        revision=revision,
    )

    first = execute(program)
    assert first.returncode == 0, first.stderr.decode()
    first_result = parse_result(first.stdout)
    assert first_result.workdir == str(root / revision)
    assert first_result.reused is False

    second = execute(program)
    assert second.returncode == 0, second.stderr.decode()
    assert parse_result(second.stdout).reused is True


def test_rejects_dirty_existing_worktree(tmp_path: Path) -> None:
    _source, bare, revision = repository(tmp_path)
    root = tmp_path / "worktrees"
    program = build_program(
        bare_repo=str(bare),
        worktree_root=str(root),
        revision=revision,
    )
    assert execute(program).returncode == 0
    (root / revision / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    rejected = execute(program)
    assert rejected.returncode == 1
    with pytest.raises(RuntimeError, match="dirty"):
        parse_result(rejected.stdout)
