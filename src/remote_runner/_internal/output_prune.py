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


RESULT_PREFIX = "RR_OUTPUT_PRUNE_RESULT "

PRUNE_OUTPUT_PROGRAM = r"""import json
import shutil
from pathlib import Path, PurePosixPath

def emit(ok, action, message=None):
    print("RR_OUTPUT_PRUNE_RESULT " + json.dumps({
        "ok": ok,
        "action": action,
        "message": message,
    }, sort_keys=True), flush=True)

def absolute_path(value, field):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(field + " must be a non-empty path")
    parsed = PurePosixPath(value)
    if not parsed.is_absolute() or str(parsed) != value or ".." in parsed.parts:
        raise ValueError(field + " must be a normalized absolute path")
    return Path(value)

try:
    output = absolute_path(payload.get("output_path"), "output_path")
    output_root = absolute_path(payload.get("output_root"), "output_root")
    workdir = absolute_path(payload.get("remote_workdir"), "remote_workdir")
except ValueError as exc:
    emit(False, "validation", str(exc))
    raise SystemExit(1)

home = Path.home()
rr_root = home / ".rr"
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
if output == output_root or output_root not in output.parents:
    emit(False, "validation", "output path is not a child of output_root")
    raise SystemExit(1)
if output_root.is_symlink() or output_root.resolve(strict=False) != output_root:
    emit(False, "validation", "output_root traverses a symlink")
    raise SystemExit(1)
if output.exists() or output.is_symlink():
    if output.is_symlink():
        emit(False, "validation", "output path is a symlink")
        raise SystemExit(1)
    if output.resolve(strict=False) != output:
        emit(False, "validation", "output path traverses a symlink")
        raise SystemExit(1)

if output.is_dir():
    shutil.rmtree(output)
    emit(True, "removed_directory")
elif output.exists():
    output.unlink()
    emit(True, "removed_file")
else:
    emit(True, "already_absent")
"""


class OutputPruneOutcomeUnknown(RuntimeError):
    pass


def build_prune_stdin(
    *,
    output_path: str,
    output_root: str | None,
    remote_workdir: str,
) -> bytes:
    payload = base64.b64encode(
        json.dumps(
            {
                "output_path": output_path,
                "output_root": output_root,
                "remote_workdir": remote_workdir,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    return (
        f"import base64, json\npayload=json.loads(base64.b64decode({payload!r}))\n"
        + PRUNE_OUTPUT_PROGRAM
    ).encode("utf-8")


def _prune_result(stdout: bytes) -> dict[str, Any] | None:
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            value = json.loads(line[len(RESULT_PREFIX) :])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


def prune_remote_output(
    *,
    ssh: str,
    python: str,
    output_path: str,
    output_root: str | None,
    remote_workdir: str,
    timeout: int,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("output prune timeout must be positive")
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
            input=build_prune_stdin(
                output_path=output_path,
                output_root=output_root,
                remote_workdir=remote_workdir,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout + 300,
        )
    except subprocess.TimeoutExpired as exc:
        raise OutputPruneOutcomeUnknown(
            f"remote output prune timed out after {exc.timeout}s; outcome is unknown"
        ) from exc
    result = _prune_result(completed.stdout)
    if completed.returncode == 0 and result is not None and result.get("ok") is True:
        return result
    detail = completed.stderr.decode(errors="replace").strip()
    raise OutputPruneOutcomeUnknown(
        str(result.get("message"))
        if result is not None and result.get("message")
        else detail or f"remote output prune exited {completed.returncode}"
    )


def request_output_prune(args: argparse.Namespace) -> dict[str, Any]:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    action_args: list[str] = []
    if args.run_id is not None:
        action_args.extend(("--run-id", validate_current_run_id(args.run_id)))
    requested_servers = getattr(args, "server", None) or ()
    configured_sources = (
        set() if config.output_sync is None else set(config.output_sync.source_hosts)
    )
    unknown_servers = sorted(set(requested_servers) - configured_sources)
    if unknown_servers:
        raise ValueError(
            "output prune servers must name configured output-sync sources: "
            + ", ".join(unknown_servers)
        )
    for server in requested_servers:
        action_args.extend(("--server", server))
    if args.apply:
        action_args.append("--apply")
    return call_controller(
        config,
        "prune-outputs",
        timeout=args.timeout,
        action_args=tuple(action_args),
        overall_timeout=max(3600, args.timeout + 300),
    )
