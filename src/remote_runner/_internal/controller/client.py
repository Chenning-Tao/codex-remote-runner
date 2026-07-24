from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any

from ..config import ManagedProjectConfig
from ..remote_shell import ssh_connection_options
from .layout import controller_release_layout


def _controller_failure_detail(
    *,
    action: str,
    ssh_target: str,
    stderr: str,
) -> str:
    detail = stderr.strip() or f"controller {action} failed"
    if (
        os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1"
        and "operation not permitted" in detail.lower()
    ):
        return (
            f"controller {action} SSH was blocked by the local Codex network "
            "sandbox; rerun the same lifecycle command with network-enabled "
            f"approval. OpenSSH may display the IP resolved from {ssh_target!r}; "
            "that does not mean the SSH alias or config was bypassed. "
            f"Original SSH error: {detail}"
        )
    return detail


def call_controller(
    config: ManagedProjectConfig,
    action: str,
    *,
    timeout: int,
    action_args: tuple[str, ...] = (),
    payload: dict[str, Any] | None = None,
    overall_timeout: int | None = None,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("controller timeout must be positive")
    if overall_timeout is not None and overall_timeout <= 0:
        raise ValueError("controller overall timeout must be positive")
    layout = controller_release_layout(config.controller.root)
    remote_argv = [
        layout.interpreter,
        "-m",
        "remote_runner._internal.controller",
        "--controller-root",
        config.controller.root,
        "--project-id",
        config.project_id,
        "--timeout",
        str(timeout),
        "--interval",
        str(config.scheduling.probe_interval_seconds),
        action,
        *action_args,
    ]
    ssh_argv = [
        "ssh",
        *ssh_connection_options(timeout),
    ]
    if overall_timeout is not None:
        ssh_argv.extend(
            [
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=4",
            ]
        )
    ssh_argv.extend([config.controller.ssh, shlex.join(remote_argv)])
    stdin = None
    if payload is not None:
        stdin = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    try:
        completed = subprocess.run(
            ssh_argv,
            input=stdin,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=overall_timeout if overall_timeout is not None else timeout + 120,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"controller {action} timed out after {exc.timeout}s") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            _controller_failure_detail(
                action=action,
                ssh_target=config.controller.ssh,
                stderr=completed.stderr,
            )
        )
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"controller {action} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"controller {action} returned invalid data")
    return value
