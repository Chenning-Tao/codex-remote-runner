from __future__ import annotations

import shutil
from pathlib import Path


TMUX_FALLBACKS = (
    Path("/opt/homebrew/bin/tmux"),
    Path("/usr/local/bin/tmux"),
    Path("/usr/bin/tmux"),
)


def resolve_tmux_executable() -> str:
    discovered = shutil.which("tmux")
    if discovered is not None:
        return discovered
    for candidate in TMUX_FALLBACKS:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("tmux is required on the controller")


def run_tmux_session(run_id: str) -> str:
    return run_id


def dispatcher_tmux_session(project_id: str) -> str:
    return f"rr-dispatch-{project_id}"


def output_sync_tmux_session(project_id: str) -> str:
    return f"rr-output-sync-{project_id}"


def exact_tmux_target(session_name: str) -> str:
    return f"={session_name}"
