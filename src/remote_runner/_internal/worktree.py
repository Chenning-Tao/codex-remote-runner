from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath

from .remote_shell import remote_python_stdin_command


RESULT_PREFIX = "RR_WORKTREE_RESULT "


WORKTREE_PROGRAM = r'''import base64
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

def emit(ok, message=None, workdir=None, reused=False):
    print("RR_WORKTREE_RESULT " + json.dumps({
        "ok": ok,
        "message": message,
        "workdir": workdir,
        "reused": reused,
    }, sort_keys=True), flush=True)

def run(args):
    return subprocess.run(
        args,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )

payload = json.loads(base64.b64decode(PAYLOAD_B64).decode("utf-8"))
bare_repo = payload.get("bare_repo")
worktree_root = payload.get("worktree_root")
revision = payload.get("revision")
for field, value in (("bare_repo", bare_repo), ("worktree_root", worktree_root)):
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute():
        emit(False, field + " must be an absolute path")
        raise SystemExit(1)
if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
    emit(False, "revision must be a full Git SHA")
    raise SystemExit(1)

bare = Path(bare_repo)
root = Path(worktree_root)
workdir = root / revision
if not bare.is_dir():
    emit(False, "bare repository does not exist: " + str(bare))
    raise SystemExit(1)
is_bare = run(["git", "--git-dir", str(bare), "rev-parse", "--is-bare-repository"])
if is_bare.returncode != 0 or is_bare.stdout.strip() != "true":
    emit(False, "configured repository is not bare")
    raise SystemExit(1)
resolved = run(["git", "--git-dir", str(bare), "rev-parse", "--verify", revision + "^{commit}"])
if resolved.returncode != 0 or resolved.stdout.strip() != revision:
    emit(False, "revision is not prepared in bare repository")
    raise SystemExit(1)

root.mkdir(parents=True, exist_ok=True)
reused = workdir.exists()
if not reused:
    added = run([
        "git", "--git-dir", str(bare), "worktree", "add", "--detach", str(workdir), revision
    ])
    if added.returncode != 0:
        emit(False, added.stderr.strip() or "git worktree add failed")
        raise SystemExit(1)
if not workdir.is_dir():
    emit(False, "worktree path is not a directory")
    raise SystemExit(1)
head = run(["git", "-C", str(workdir), "rev-parse", "HEAD"])
if head.returncode != 0 or head.stdout.strip() != revision:
    emit(False, "worktree revision mismatch")
    raise SystemExit(1)
status = run(["git", "-C", str(workdir), "status", "--porcelain", "--untracked-files=normal"])
if status.returncode != 0:
    emit(False, status.stderr.strip() or "worktree status failed")
    raise SystemExit(1)
if status.stdout:
    emit(False, "worktree is dirty")
    raise SystemExit(1)
emit(True, workdir=str(workdir), reused=reused)
'''


@dataclass(frozen=True)
class WorktreeResult:
    workdir: str
    reused: bool


def build_program(*, bare_repo: str, worktree_root: str, revision: str) -> bytes:
    for field, value in (("bare_repo", bare_repo), ("worktree_root", worktree_root)):
        if not PurePosixPath(value).is_absolute():
            raise ValueError(f"{field} must be an absolute POSIX path")
    payload = json.dumps(
        {
            "bare_repo": bare_repo,
            "worktree_root": worktree_root,
            "revision": revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    encoded = base64.b64encode(payload).decode("ascii")
    return (f"PAYLOAD_B64 = {encoded!r}\n" + WORKTREE_PROGRAM).encode()


def parse_result(stdout: bytes) -> WorktreeResult:
    result: dict[str, object] | None = None
    for line in stdout.decode(errors="replace").splitlines():
        if line.startswith(RESULT_PREFIX):
            value = json.loads(line[len(RESULT_PREFIX) :])
            if isinstance(value, dict):
                result = value
    if result is None:
        raise RuntimeError("remote worktree bootstrap returned no structured result")
    if result.get("ok") is not True:
        raise RuntimeError(str(result.get("message") or "remote worktree preparation failed"))
    workdir = result.get("workdir")
    if not isinstance(workdir, str) or not PurePosixPath(workdir).is_absolute():
        raise RuntimeError("remote worktree result has invalid workdir")
    return WorktreeResult(workdir=workdir, reused=result.get("reused") is True)


def prepare_remote_worktree(
    *,
    ssh: str,
    python: str,
    bare_repo: str,
    worktree_root: str,
    revision: str,
    timeout: int = 8,
) -> WorktreeResult:
    if timeout <= 0:
        raise ValueError("worktree preparation timeout must be positive")
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={timeout}",
        ssh,
        remote_python_stdin_command(python),
    ]
    completed = subprocess.run(
        argv,
        input=build_program(
            bare_repo=bare_repo,
            worktree_root=worktree_root,
            revision=revision,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout + 95,
    )
    try:
        return parse_result(completed.stdout)
    except (json.JSONDecodeError, RuntimeError) as exc:
        detail = completed.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or str(exc)) from exc
