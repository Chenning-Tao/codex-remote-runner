from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .remote_shell import ssh_connection_options


FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class CommandRunner(Protocol):
    def __call__(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True)
class DeploymentTarget:
    name: str
    remote_url: str


@dataclass(frozen=True)
class PreparedServer:
    name: str
    remote_url: str
    ref: str
    revision: str


@dataclass(frozen=True)
class PreparationFailure:
    name: str
    error: str


@dataclass(frozen=True)
class PreparationResult:
    revision: str
    ref: str
    prepared: tuple[PreparedServer, ...]
    failures: tuple[PreparationFailure, ...]

    @property
    def prepared_servers(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.prepared)


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _checked(
    runner: CommandRunner,
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 30,
) -> str:
    completed = runner(args, cwd=cwd, timeout=timeout)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(detail or f"command exited {completed.returncode}: {args[0]}")
    return completed.stdout.strip()


def resolve_clean_head(source_repo: Path, *, runner: CommandRunner = _run) -> str:
    resolved = source_repo.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"local source repository does not exist: {resolved}")
    top = Path(
        _checked(
            runner,
            ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        )
    ).resolve()
    if top != resolved:
        raise ValueError(f"configured local source must be the Git worktree root: {resolved}")
    revision = _checked(
        runner,
        ["git", "-C", str(resolved), "rev-parse", "--verify", "HEAD^{commit}"],
    )
    if not FULL_SHA_RE.fullmatch(revision):
        raise RuntimeError(f"local Git HEAD is not a full commit SHA: {revision!r}")
    status = _checked(
        runner,
        ["git", "-C", str(resolved), "status", "--porcelain", "--untracked-files=normal"],
    )
    if status:
        raise ValueError("local source repository must be clean before remote submission")
    return revision


def runner_ref(project_id: str, revision: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project_id):
        raise ValueError("invalid project id for runner ref")
    if not FULL_SHA_RE.fullmatch(revision):
        raise ValueError("revision must be a full lowercase Git SHA")
    return f"refs/remote-runner/{project_id}/{revision}"


def _git_with_managed_ssh(timeout: int) -> list[str]:
    ssh_command = shlex.join(["ssh", *ssh_connection_options(timeout)])
    return ["git", "-c", f"core.sshCommand={ssh_command}"]


def prepare_revision(
    source_repo: Path,
    *,
    project_id: str,
    targets: list[DeploymentTarget],
    explicit_server: str | None = None,
    revision: str | None = None,
    timeout: int = 60,
    runner: CommandRunner = _run,
) -> PreparationResult:
    if timeout <= 0:
        raise ValueError("preparation timeout must be positive")
    if not targets:
        raise ValueError("at least one deployment target is required")
    head = resolve_clean_head(source_repo, runner=runner)
    if revision is None:
        revision = head
    else:
        if not FULL_SHA_RE.fullmatch(revision):
            raise ValueError("revision must be a full lowercase Git SHA")
        resolved_revision = _checked(
            runner,
            [
                "git",
                "-C",
                str(source_repo),
                "rev-parse",
                "--verify",
                f"{revision}^{{commit}}",
            ],
        )
        if resolved_revision != revision:
            raise RuntimeError(
                f"local Git revision did not resolve exactly: {resolved_revision!r}"
            )
    ref = runner_ref(project_id, revision)
    prepared: list[PreparedServer] = []
    failures: list[PreparationFailure] = []

    for target in targets:
        try:
            _checked(
                runner,
                [
                    *_git_with_managed_ssh(timeout),
                    "-C",
                    str(source_repo),
                    "push",
                    "--porcelain",
                    target.remote_url,
                    f"{revision}:{ref}",
                ],
                timeout=timeout,
            )
            listed = _checked(
                runner,
                [
                    *_git_with_managed_ssh(timeout),
                    "ls-remote",
                    "--exit-code",
                    target.remote_url,
                    ref,
                ],
                timeout=timeout,
            )
            fields = listed.split()
            if len(fields) != 2 or fields[0] != revision or fields[1] != ref:
                raise RuntimeError(
                    f"remote ref verification mismatch for {target.name}: {listed!r}"
                )
            prepared.append(
                PreparedServer(
                    name=target.name,
                    remote_url=target.remote_url,
                    ref=ref,
                    revision=revision,
                )
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            failures.append(PreparationFailure(name=target.name, error=str(exc)))

    if explicit_server is not None:
        if len(targets) != 1 or targets[0].name != explicit_server:
            raise ValueError("explicit preparation must contain exactly the requested server")
        if not prepared:
            raise RuntimeError(
                f"failed to prepare explicit server {explicit_server}: {failures[0].error}"
            )
    elif not prepared:
        details = "; ".join(f"{item.name}: {item.error}" for item in failures)
        raise RuntimeError(f"failed to prepare any automatic server: {details}")

    return PreparationResult(
        revision=revision,
        ref=ref,
        prepared=tuple(prepared),
        failures=tuple(failures),
    )
