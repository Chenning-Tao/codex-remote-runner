from __future__ import annotations

import argparse
import base64
import json
import subprocess
from typing import Any

from .config import load_managed_project_config
from .controller.client import call_controller
from .execution_registry import (
    ProjectPaths,
    load_current_run,
    registry_kind,
    resolve_project_config,
    run_lock,
    update_current_state,
    utc_now,
    validate_current_run_id,
)
from .remote_shell import remote_python_stdin_command


STOP_PROGRAM = r'''import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

RUN_ID_RE = re.compile(r"^rr-[0-9a-f]{16}$")
TERMINAL = {"succeeded", "failed", "stopped"}

def exact_tmux_target(session_name):
    return "=" + session_name

def now():
    return datetime.now(timezone.utc).isoformat()

def emit(ok, action, message=None, status=None):
    print("RR_STOP_RESULT " + json.dumps({
        "ok": ok,
        "action": action,
        "message": message,
        "status": status,
    }, sort_keys=True), flush=True)

def read_status(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None

def write_status(path, value):
    temporary = path.with_name(".status.stop.%d" % os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError("write returned zero bytes")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)

def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def group_owned(pgid, run_id, runtime):
    owner = read_status(runtime / "owner.json")
    if not isinstance(owner, dict) or owner.get("run_id") != run_id:
        return False
    if owner.get("pid") != pgid or owner.get("pgid") != pgid:
        return False
    try:
        listed = subprocess.run(
            ["ps", "-ww", "-p", str(pgid), "-o", "pid=,pgid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return False
    for line in listed.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if (
            len(fields) == 3
            and fields[1] == str(pgid)
            and fields[0] == str(pgid)
            and str(run_id) in fields[2]
            and str(runtime) in fields[2]
        ):
            return True
    return False

def kill_tmux_and_confirm(run_id):
    target = exact_tmux_target(run_id)
    subprocess.run(
        ["tmux", "kill-session", "-t", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return subprocess.run(
        ["tmux", "has-session", "-t", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode != 0

run_id = payload.get("run_id")
timeout = payload.get("timeout")
if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
    emit(False, "validation", "invalid run id")
    raise SystemExit(1)
if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
    emit(False, "validation", "invalid stop timeout")
    raise SystemExit(1)

runtime = Path.home() / ".rr" / run_id
status_path = runtime / "status.json"
existing = read_status(status_path)
valid_terminal = (
    existing is not None
    and existing.get("run_id") == run_id
    and existing.get("state") in TERMINAL
)
if not runtime.is_dir():
    emit(
        True,
        "already_terminal" if valid_terminal else "not_started",
        status=existing if valid_terminal else None,
    )
    raise SystemExit(0)

pgid_path = runtime / "pgid"
try:
    pgid = int(pgid_path.read_text(encoding="utf-8").strip())
except (FileNotFoundError, OSError, ValueError):
    alive = subprocess.run(
        ["tmux", "has-session", "-t", exact_tmux_target(run_id)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if alive:
        emit(False, "unknown", "runtime has a live tmux session but no valid pgid")
        raise SystemExit(2)
    if valid_terminal:
        emit(True, "already_terminal", status=existing)
        raise SystemExit(0)
    fallback = {
        "schema_version": 1,
        "run_id": run_id,
        "workload_class": "standard" if existing is None else existing.get("workload_class", "standard"),
        "state": "stopped",
        "exit_code": None,
        "started_at": None if existing is None else existing.get("started_at"),
        "finished_at": now(),
    }
    write_status(status_path, fallback)
    emit(True, "stopped_before_workload", status=fallback)
    raise SystemExit(0)
if pgid <= 1:
    emit(False, "validation", "refusing unsafe process-group id")
    raise SystemExit(1)

if not group_alive(pgid):
    if not kill_tmux_and_confirm(run_id):
        emit(False, "unknown", "tmux session is still alive after process-group exit")
        raise SystemExit(2)
    if valid_terminal:
        emit(True, "already_terminal", status=existing)
    else:
        fallback = {
            "schema_version": 1,
            "run_id": run_id,
            "workload_class": "standard" if existing is None else existing.get("workload_class", "standard"),
            "state": "stopped",
            "exit_code": None,
            "started_at": None if existing is None else existing.get("started_at"),
            "finished_at": now(),
        }
        write_status(status_path, fallback)
        emit(True, "stopped_before_workload", status=fallback)
    raise SystemExit(0)

if not group_owned(pgid, run_id, runtime):
    emit(False, "unknown", "process group identity does not match this run")
    raise SystemExit(2)

if valid_terminal and not group_alive(pgid):
    emit(True, "already_terminal", status=existing)
    raise SystemExit(0)

stop_path = runtime / "stop.request"
fd = os.open(stop_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.fchmod(fd, 0o600)
os.close(fd)
try:
    os.killpg(pgid, signal.SIGTERM)
except ProcessLookupError:
    pass

deadline = time.monotonic() + float(timeout)
while time.monotonic() < deadline:
    current = read_status(status_path)
    alive = group_alive(pgid)
    if current is not None and current.get("state") in TERMINAL and not alive:
        break
    if not alive:
        break
    time.sleep(0.05)
if group_alive(pgid):
    if not group_owned(pgid, run_id, runtime):
        emit(False, "unknown", "process group identity changed before escalation")
        raise SystemExit(2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass

deadline = time.monotonic() + 5.0
while time.monotonic() < deadline:
    current = read_status(status_path)
    alive = group_alive(pgid)
    if current is not None and current.get("state") in TERMINAL and not alive:
        break
    time.sleep(0.05)
if group_alive(pgid):
    emit(False, "unknown", "process group is still alive after SIGKILL")
    raise SystemExit(2)

if not kill_tmux_and_confirm(run_id):
    emit(False, "unknown", "tmux session is still alive after process-group stop")
    raise SystemExit(2)
current = read_status(status_path)
if current is not None and current.get("state") in TERMINAL:
    emit(True, "stopped" if current.get("state") == "stopped" else "already_terminal", status=current)
    raise SystemExit(0)
fallback = {
    "schema_version": 1,
    "run_id": run_id,
    "workload_class": "standard" if existing is None else existing.get("workload_class", "standard"),
    "state": "stopped",
    "exit_code": 143,
    "started_at": None if existing is None else existing.get("started_at"),
    "finished_at": now(),
}
write_status(status_path, fallback)
emit(True, "stopped_fallback", status=fallback)
'''


class StopOutcomeUnknown(RuntimeError):
    pass


# The remote stopper waits this long after escalation for the process group to disappear.
REMOTE_KILL_BUDGET_SECONDS = 5
TRANSPORT_MARGIN_SECONDS = 10


def build_stop_stdin(run_id: str, timeout: float) -> bytes:
    payload = base64.b64encode(
        json.dumps({"run_id": run_id, "timeout": timeout}, separators=(",", ":")).encode()
    ).decode()
    return (
        f"import base64, json\npayload=json.loads(base64.b64decode({payload!r}))\n"
        + STOP_PROGRAM
    ).encode()


def _stop_result(stdout: bytes) -> dict[str, Any] | None:
    prefix = "RR_STOP_RESULT "
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        if not line.startswith(prefix):
            continue
        try:
            value = json.loads(line[len(prefix) :])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def execute_stop(
    ssh_target: str,
    project_python: str,
    run_id: str,
    timeout: int,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("stop timeout must be positive")
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
            input=build_stop_stdin(run_id, timeout),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=(2 * timeout) + REMOTE_KILL_BUDGET_SECONDS + TRANSPORT_MARGIN_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise StopOutcomeUnknown(
            f"remote stop timed out after {exc.timeout}s; stop outcome is unknown"
        ) from exc
    result = _stop_result(completed.stdout)
    if completed.returncode == 0 and result is not None and result.get("ok") is True:
        return result
    detail = completed.stderr.decode(errors="replace").strip() or completed.stdout.decode(
        errors="replace"
    ).strip()
    raise StopOutcomeUnknown(
        str(result.get("message"))
        if result is not None and result.get("message")
        else detail or f"remote stop exited {completed.returncode}"
    )


def _changes_from_stop(result: dict[str, Any]) -> dict[str, Any]:
    remote = result.get("status")
    if not isinstance(remote, dict):
        return {
            "status": "stopped",
            "finished_at": utc_now(),
            "exit_code": None,
            "error": None,
        }
    status = remote.get("state")
    if status not in {"succeeded", "failed", "stopped"}:
        status = "stopped"
    return {
        "status": status,
        "started_at": remote.get("started_at"),
        "finished_at": remote.get("finished_at") or utc_now(),
        "exit_code": remote.get("exit_code"),
        "error": None,
    }


def stop(paths: ProjectPaths, run_id: str, timeout: int) -> dict[str, Any]:
    validate_current_run_id(run_id)
    if registry_kind(paths, run_id) != "current":
        raise ValueError("only current-format runs can be stopped")
    with run_lock(paths, run_id):
        manifest, state = load_current_run(paths, run_id)
        if state["status"] in {"succeeded", "failed", "stopped"}:
            return state
        try:
            result = execute_stop(
                str(manifest["ssh"]),
                str(manifest["project_python"]),
                run_id,
                timeout,
            )
        except (OSError, StopOutcomeUnknown) as exc:
            state = update_current_state(
                paths,
                run_id,
                int(state["revision"]),
                {"status": state["status"], "error": str(exc)},
                action="stop_outcome_unknown",
                lock_held=True,
            )
            raise RuntimeError(str(exc)) from exc
        return update_current_state(
            paths,
            run_id,
            int(state["revision"]),
            _changes_from_stop(result),
            action="stopped",
            lock_held=True,
        )


def request_stop(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    return call_controller(
        config,
        "stop",
        timeout=args.timeout,
        action_args=("--run-id", args.run_id),
    )
