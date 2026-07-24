from __future__ import annotations

import contextlib
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .wakeup import WakeupPaths


LAUNCHD_LABEL = "com.openai.codex-remote-runner-wakeup"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def launch_agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _service_target() -> str:
    return f"{_domain()}/{LAUNCHD_LABEL}"


def _plist(paths: WakeupPaths) -> dict[str, Any]:
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "remote_runner.cli",
            "wakeup",
            "worker",
            "--supervised",
            "--state-root",
            str(paths.root),
        ],
        "KeepAlive": {"PathState": {str(paths.pending_marker): True}},
        "ProcessType": "Background",
        "ThrottleInterval": 5,
        "StandardOutPath": str(paths.root / "worker.stdout.log"),
        "StandardErrorPath": str(paths.root / "worker.stderr.log"),
    }


def _run_launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def supervisor_status(paths: WakeupPaths) -> dict[str, Any]:
    plist_path = launch_agent_path()
    available = _is_macos()
    service_loaded = False
    if available:
        service_loaded = _run_launchctl("print", _service_target()).returncode == 0
    configured_root: str | None = None
    if plist_path.is_file() and not plist_path.is_symlink():
        try:
            payload = plistlib.loads(plist_path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            payload = None
        if isinstance(payload, dict):
            arguments = payload.get("ProgramArguments")
            if isinstance(arguments, list) and "--state-root" in arguments:
                index = arguments.index("--state-root")
                if index + 1 < len(arguments) and isinstance(arguments[index + 1], str):
                    configured_root = arguments[index + 1]
    matches = configured_root == str(paths.root)
    return {
        "available": available,
        "installed": plist_path.is_file() and matches,
        "loaded": service_loaded and matches,
        "service_loaded": service_loaded,
        "plist": str(plist_path),
        "state_root": configured_root,
    }


def _write_plist_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def install(paths: WakeupPaths) -> dict[str, Any]:
    if not _is_macos():
        raise RuntimeError("wakeup restart recovery currently requires macOS launchd")
    paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.root, 0o700)
    plist_path = launch_agent_path()
    desired = _plist(paths)
    status = supervisor_status(paths)
    current: dict[str, Any] | None = None
    if plist_path.is_file() and not plist_path.is_symlink():
        try:
            loaded = plistlib.loads(plist_path.read_bytes())
        except plistlib.InvalidFileException:
            loaded = None
        if isinstance(loaded, dict):
            current = loaded
    if current == desired and status["loaded"] is True:
        return {"status": "already_installed", **status}

    if status["service_loaded"] is True:
        stopped = _run_launchctl("bootout", _service_target())
        if stopped.returncode != 0:
            raise RuntimeError(
                stopped.stderr.strip() or "failed to stop the existing wakeup supervisor"
            )
    _write_plist_atomic(plist_path, desired)
    enabled = _run_launchctl("enable", _service_target())
    if enabled.returncode != 0:
        raise RuntimeError(
            enabled.stderr.strip() or "failed to enable the wakeup supervisor"
        )
    started = _run_launchctl("bootstrap", _domain(), str(plist_path))
    if started.returncode != 0:
        raise RuntimeError(
            started.stderr.strip() or "failed to bootstrap the wakeup supervisor"
        )
    return {"status": "installed", **supervisor_status(paths)}


def uninstall(paths: WakeupPaths) -> dict[str, Any]:
    if not _is_macos():
        raise RuntimeError("wakeup restart recovery currently requires macOS launchd")
    plist_path = launch_agent_path()
    status = supervisor_status(paths)
    if status["service_loaded"] is True:
        stopped = _run_launchctl("bootout", _service_target())
        if stopped.returncode != 0:
            raise RuntimeError(
                stopped.stderr.strip() or "failed to stop the wakeup supervisor"
            )
    removed = False
    if plist_path.is_file() or plist_path.is_symlink():
        plist_path.unlink()
        removed = True
    return {
        "status": (
            "uninstalled" if removed or status["service_loaded"] else "not_installed"
        ),
        **supervisor_status(paths),
    }
