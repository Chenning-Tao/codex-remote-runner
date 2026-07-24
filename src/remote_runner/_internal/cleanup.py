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


CLEANUP_PROGRAM = r"""import json
import os
import re
import shutil
import subprocess
from pathlib import Path

RUN_ID_RE = re.compile(r"^rr-[0-9a-f]{16}$")

def emit(ok, action, message=None):
    print("RR_CLEANUP_RESULT " + json.dumps({
        "ok": ok,
        "action": action,
        "message": message,
    }, sort_keys=True), flush=True)

def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

run_id = payload.get("run_id")
if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
    emit(False, "validation", "invalid run id")
    raise SystemExit(1)

rr_root = Path.home() / ".rr"
runtime = rr_root / run_id
if not runtime.exists():
    emit(True, "already_absent")
    raise SystemExit(0)
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
    emit(False, "unknown", "runtime has no valid stopped status evidence")
    raise SystemExit(2)
if not isinstance(status, dict) or status.get("run_id") != run_id:
    emit(False, "validation", "runtime status identity mismatch")
    raise SystemExit(1)
if status.get("state") != "stopped":
    emit(False, "validation", "runtime status is not stopped")
    raise SystemExit(1)

shutil.rmtree(runtime)
emit(True, "removed")
"""


class CleanupOutcomeUnknown(RuntimeError):
    pass


def build_cleanup_stdin(run_id: str) -> bytes:
    validate_current_run_id(run_id)
    payload = base64.b64encode(
        json.dumps({"run_id": run_id}, separators=(",", ":")).encode()
    ).decode()
    return (
        f"import base64, json\npayload=json.loads(base64.b64decode({payload!r}))\n"
        + CLEANUP_PROGRAM
    ).encode()


def _cleanup_result(stdout: bytes) -> dict[str, Any] | None:
    prefix = "RR_CLEANUP_RESULT "
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        if not line.startswith(prefix):
            continue
        try:
            value = json.loads(line[len(prefix) :])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def cleanup_remote_runtime(
    ssh_target: str,
    project_python: str,
    run_id: str,
    timeout: int,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("cleanup timeout must be positive")
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={timeout}",
        ssh_target,
        remote_python_stdin_command(project_python),
    ]
    try:
        completed = subprocess.run(
            argv,
            input=build_cleanup_stdin(run_id),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout + 10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CleanupOutcomeUnknown(
            f"remote cleanup timed out after {exc.timeout}s; outcome is unknown"
        ) from exc
    result = _cleanup_result(completed.stdout)
    if completed.returncode == 0 and result is not None and result.get("ok") is True:
        return result
    detail = (
        completed.stderr.decode(errors="replace").strip()
        or completed.stdout.decode(errors="replace").strip()
    )
    raise CleanupOutcomeUnknown(
        str(result.get("message"))
        if result is not None and result.get("message")
        else detail or f"remote cleanup exited {completed.returncode}"
    )


def request_cleanup(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    action_args: list[str] = []
    if args.run_id is not None:
        validate_current_run_id(args.run_id)
        action_args.extend(("--run-id", args.run_id))
    if args.apply:
        action_args.append("--apply")
    return call_controller(
        config,
        "cleanup-stopped",
        timeout=args.timeout,
        action_args=tuple(action_args),
    )
