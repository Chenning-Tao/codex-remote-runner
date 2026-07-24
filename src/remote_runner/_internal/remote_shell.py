from __future__ import annotations

import shlex
import subprocess


def ssh_capture(
    ssh_target: str,
    remote_command: str,
    timeout: int,
    *,
    input_text: str | None = None,
) -> tuple[int, str, str]:
    remote_shell_command = f"bash -c {shlex.quote(remote_command)}"
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={timeout}",
        ssh_target,
        remote_shell_command,
    ]
    process_timeout = max(timeout + 2, 5)
    try:
        completed = subprocess.run(
            command,
            check=False,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=process_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout
            if isinstance(exc.stdout, str)
            else (exc.stdout or b"").decode(errors="replace")
        )
        stderr = (
            exc.stderr
            if isinstance(exc.stderr, str)
            else (exc.stderr or b"").decode(errors="replace")
        )
        timeout_message = f"ssh probe timed out after {process_timeout}s"
        if stderr:
            stderr = f"{stderr.rstrip()}\n{timeout_message}"
        else:
            stderr = timeout_message
        return 124, stdout, stderr
    return completed.returncode, completed.stdout, completed.stderr


def shell_quote_remote_path(path: str) -> str:
    if path.startswith("~/"):
        return "$HOME/" + shlex.quote(path[2:])
    return shlex.quote(path)


def remote_python_stdin_command(python_path: str, *, no_site: bool = False) -> str:
    arguments = [python_path]
    if no_site:
        arguments.append("-S")
    arguments.append("-")
    return shlex.join(arguments)
