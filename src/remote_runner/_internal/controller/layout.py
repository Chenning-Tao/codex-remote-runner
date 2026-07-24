from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class ControllerReleaseLayout:
    root: str
    runner_root: str
    releases_root: str
    current: str
    interpreter: str


def controller_release_layout(controller_root: str) -> ControllerReleaseLayout:
    root = PurePosixPath(controller_root)
    if not root.is_absolute():
        raise ValueError("controller root must be an absolute POSIX path")
    runner_root = root / "runner"
    current = runner_root / "current"
    return ControllerReleaseLayout(
        root=str(root),
        runner_root=str(runner_root),
        releases_root=str(runner_root / "releases"),
        current=str(current),
        interpreter=str(current / "venv" / "bin" / "python"),
    )
