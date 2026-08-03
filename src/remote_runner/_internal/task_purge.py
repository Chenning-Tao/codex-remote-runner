from __future__ import annotations

import argparse
import base64
import json
import subprocess
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import resolve_project_config, validate_current_run_id
from .remote_shell import remote_python_stdin_command


RESULT_PREFIX = "RR_TASK_PURGE_RESULT "

PURGE_RUN_PROGRAM = r"""import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

RUN_ID_RE = re.compile(r"^rr-[0-9a-f]{16}$")
TERMINAL = {"succeeded", "failed", "stopped"}

def emit(ok, action, message=None, **extra):
    print("RR_TASK_PURGE_RESULT " + json.dumps({
        "ok": ok,
        "action": action,
        "message": message,
        **extra,
    }, sort_keys=True), flush=True)

def absolute_path(value, field):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(field + " must be a non-empty path")
    parsed = PurePosixPath(value)
    if not parsed.is_absolute() or str(parsed) != value or ".." in parsed.parts:
        raise ValueError(field + " must be a normalized absolute path")
    return Path(value)

def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

run_id = payload.get("run_id")
expected_state = payload.get("expected_state")
if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
    emit(False, "validation", "invalid run id")
    raise SystemExit(1)
if expected_state not in TERMINAL:
    emit(False, "validation", "expected state is not terminal")
    raise SystemExit(1)

home = Path.home()
rr_root = home / ".rr"
runtime = rr_root / run_id
runtime_exists = runtime.exists()
if runtime_exists:
    if rr_root.is_symlink() or runtime.is_symlink() or not runtime.is_dir():
        emit(False, "validation", "runtime path is not a private directory")
        raise SystemExit(1)
    try:
        tmux_alive = subprocess.run(
            ["tmux", "has-session", "-t", "=" + run_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError as exc:
        emit(False, "unknown", "cannot verify tmux state: " + str(exc))
        raise SystemExit(2)
    if tmux_alive:
        emit(False, "active", "runtime still has a tmux session")
        raise SystemExit(2)
    try:
        pgid = int((runtime / "pgid").read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        pgid = None
    except (OSError, ValueError):
        emit(False, "unknown", "runtime has an invalid process-group record")
        raise SystemExit(2)
    if pgid is not None and (pgid <= 1 or group_alive(pgid)):
        emit(False, "active", "runtime process group may still be alive")
        raise SystemExit(2)
    try:
        status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        emit(False, "unknown", "runtime has no valid terminal status evidence")
        raise SystemExit(2)
    if not isinstance(status, dict) or status.get("run_id") != run_id:
        emit(False, "validation", "runtime status identity mismatch")
        raise SystemExit(1)
    if status.get("state") != expected_state:
        emit(False, "validation", "runtime status does not match controller state")
        raise SystemExit(1)

output = None
raw_output = payload.get("output_path")
raw_root = payload.get("output_root")
workdir = absolute_path(payload.get("remote_workdir"), "remote_workdir")
if raw_output is not None:
    try:
        output = absolute_path(raw_output, "output_path")
        output_root = None if raw_root is None else absolute_path(raw_root, "output_root")
    except ValueError as exc:
        emit(False, "validation", str(exc))
        raise SystemExit(1)
    if len(output.parts) < 3:
        emit(False, "validation", "output path is too broad for recursive deletion")
        raise SystemExit(1)
    if output in {Path("/"), home, rr_root, workdir}:
        emit(False, "validation", "output path is a protected root")
        raise SystemExit(1)
    if output in home.parents or output in rr_root.parents or output in workdir.parents:
        emit(False, "validation", "output path contains a protected runtime path")
        raise SystemExit(1)
    if rr_root in output.parents:
        emit(False, "validation", "output path is inside the runtime root")
        raise SystemExit(1)
    if output_root is not None:
        if output == output_root or output_root not in output.parents:
            emit(False, "validation", "output path is not a child of output_root")
            raise SystemExit(1)
        if output_root.resolve(strict=False) != output_root:
            emit(False, "validation", "output_root traverses a symlink")
            raise SystemExit(1)
    if output.exists() or output.is_symlink():
        if output.is_symlink():
            emit(False, "validation", "output path is a symlink")
            raise SystemExit(1)
        if output.resolve(strict=False) != output:
            emit(False, "validation", "output path traverses a symlink")
            raise SystemExit(1)

output_action = "not_declared"
if output is not None:
    if output.is_dir():
        shutil.rmtree(output)
        output_action = "removed_directory"
    elif output.exists():
        output.unlink()
        output_action = "removed_file"
    else:
        output_action = "already_absent"

runtime_action = "already_absent"
if runtime_exists:
    shutil.rmtree(runtime)
    runtime_action = "removed"
emit(
    True,
    "purged",
    runtime_action=runtime_action,
    output_action=output_action,
)
"""

PURGE_WORKTREE_PROGRAM = r"""import json
import re
import subprocess
from pathlib import Path, PurePosixPath

def emit(ok, action, message=None):
    print("RR_TASK_PURGE_RESULT " + json.dumps({
        "ok": ok,
        "action": action,
        "message": message,
    }, sort_keys=True), flush=True)

def absolute_path(value, field):
    if not isinstance(value, str) or not value:
        raise ValueError(field + " must be a non-empty path")
    parsed = PurePosixPath(value)
    if not parsed.is_absolute() or str(parsed) != value or ".." in parsed.parts:
        raise ValueError(field + " must be a normalized absolute path")
    return Path(value)

try:
    bare = absolute_path(payload.get("bare_repo"), "bare_repo")
    root = absolute_path(payload.get("worktree_root"), "worktree_root")
    workdir = absolute_path(payload.get("remote_workdir"), "remote_workdir")
except ValueError as exc:
    emit(False, "validation", str(exc))
    raise SystemExit(1)
revision = payload.get("revision")
if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
    emit(False, "validation", "revision must be a full Git SHA")
    raise SystemExit(1)
if workdir != root / revision:
    emit(False, "validation", "worktree path does not match root and revision")
    raise SystemExit(1)
if workdir.is_symlink() or root.is_symlink() or bare.is_symlink():
    emit(False, "validation", "worktree paths must not be symlinks")
    raise SystemExit(1)
if any(path.exists() and path.resolve() != path for path in (workdir, root, bare)):
    emit(False, "validation", "worktree paths traverse a symlink")
    raise SystemExit(1)
if not workdir.exists():
    emit(True, "already_absent")
    raise SystemExit(0)
if not workdir.is_dir() or not bare.is_dir():
    emit(False, "validation", "worktree or bare repository is not a directory")
    raise SystemExit(1)

def run(argv):
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )

head = run(["git", "-C", str(workdir), "rev-parse", "HEAD"])
if head.returncode != 0 or head.stdout.strip() != revision:
    emit(False, "validation", "worktree revision mismatch")
    raise SystemExit(1)
removed = run([
    "git", "--git-dir", str(bare), "worktree", "remove", "--force", str(workdir)
])
if removed.returncode != 0:
    emit(False, "failed", removed.stderr.strip() or "git worktree remove failed")
    raise SystemExit(2)
run(["git", "--git-dir", str(bare), "worktree", "prune"])
emit(True, "removed")
"""


class PurgeOutcomeUnknown(RuntimeError):
    pass


def _stdin(payload: dict[str, Any], program: str) -> bytes:
    encoded = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return (
        f"import base64, json\npayload=json.loads(base64.b64decode({encoded!r}))\n"
        + program
    ).encode("utf-8")


def _result(stdout: bytes) -> dict[str, Any] | None:
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            value = json.loads(line[len(RESULT_PREFIX) :])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def _invoke(
    *,
    ssh: str,
    python: str,
    payload: dict[str, Any],
    program: str,
    timeout: int,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("task purge timeout must be positive")
    try:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"ConnectTimeout={timeout}",
                ssh,
                remote_python_stdin_command(python),
            ],
            input=_stdin(payload, program),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout + 300,
        )
    except subprocess.TimeoutExpired as exc:
        raise PurgeOutcomeUnknown(
            f"remote task purge timed out after {exc.timeout}s; outcome is unknown"
        ) from exc
    result = _result(completed.stdout)
    if completed.returncode == 0 and result is not None and result.get("ok") is True:
        return result
    detail = completed.stderr.decode(errors="replace").strip()
    raise PurgeOutcomeUnknown(
        str(result.get("message"))
        if result is not None and result.get("message")
        else detail or f"remote task purge exited {completed.returncode}"
    )


def purge_remote_run_artifacts(
    *,
    ssh: str,
    python: str,
    run_id: str,
    expected_state: str,
    remote_workdir: str,
    output_root: str | None,
    output_path: str | None,
    timeout: int,
) -> dict[str, Any]:
    validate_current_run_id(run_id)
    return _invoke(
        ssh=ssh,
        python=python,
        payload={
            "run_id": run_id,
            "expected_state": expected_state,
            "remote_workdir": remote_workdir,
            "output_root": output_root,
            "output_path": output_path,
        },
        program=PURGE_RUN_PROGRAM,
        timeout=timeout,
    )


def purge_remote_worktree(
    *,
    ssh: str,
    python: str,
    bare_repo: str,
    worktree_root: str,
    remote_workdir: str,
    revision: str,
    timeout: int,
) -> dict[str, Any]:
    return _invoke(
        ssh=ssh,
        python=python,
        payload={
            "bare_repo": bare_repo,
            "worktree_root": worktree_root,
            "remote_workdir": remote_workdir,
            "revision": revision,
        },
        program=PURGE_WORKTREE_PROGRAM,
        timeout=timeout,
    )


def request_task_purge(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    action_args = ["--task-id", args.task_id, "--reason", args.reason]
    if args.apply:
        action_args.append("--apply")
    if args.delete_artifacts:
        action_args.append("--delete-artifacts")
    return call_controller(
        config,
        "purge-task",
        timeout=args.timeout,
        action_args=tuple(action_args),
        overall_timeout=max(3600, args.timeout + 300),
    )
