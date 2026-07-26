from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from remote_runner._internal import wakeup, wakeup_supervisor


def completed(argv: list[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")


def test_install_creates_an_on_demand_pathstate_launch_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = wakeup.wakeup_paths(tmp_path / "state")
    plist_path = tmp_path / "LaunchAgents" / "wakeup.plist"
    calls: list[tuple[str, ...]] = []
    loaded = False

    def launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
        nonlocal loaded
        calls.append(arguments)
        if arguments[0] == "print":
            return completed(list(arguments), 0 if loaded else 1)
        if arguments[0] == "bootstrap":
            loaded = True
        return completed(list(arguments))

    monkeypatch.setattr(wakeup_supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(wakeup_supervisor, "launch_agent_path", lambda: plist_path)
    monkeypatch.setattr(wakeup_supervisor, "_run_launchctl", launchctl)

    result = wakeup_supervisor.install(paths)

    assert result["status"] == "installed"
    assert result["loaded"] is True
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["KeepAlive"] == {
        "PathState": {str(paths.pending_marker): True}
    }
    assert payload["ProgramArguments"][-2:] == ["--state-root", str(paths.root)]
    executable_path = payload["EnvironmentVariables"]["PATH"].split(os.pathsep)
    assert str(Path(sys.executable).resolve().parent) in executable_path
    assert "/usr/bin" in executable_path
    assert "RunAtLoad" not in payload
    assert [call[0] for call in calls] == ["print", "enable", "bootstrap", "print"]


def test_install_is_idempotent_when_the_matching_agent_is_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = wakeup.wakeup_paths(tmp_path / "state")
    plist_path = tmp_path / "LaunchAgents" / "wakeup.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(plistlib.dumps(wakeup_supervisor._plist(paths)))
    calls: list[tuple[str, ...]] = []

    def launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        return completed(list(arguments))

    monkeypatch.setattr(wakeup_supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(wakeup_supervisor, "launch_agent_path", lambda: plist_path)
    monkeypatch.setattr(wakeup_supervisor, "_run_launchctl", launchctl)

    result = wakeup_supervisor.install(paths)

    assert result["status"] == "already_installed"
    assert calls == [("print", wakeup_supervisor._service_target())]


def test_uninstall_boots_out_and_removes_the_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = wakeup.wakeup_paths(tmp_path / "state")
    plist_path = tmp_path / "LaunchAgents" / "wakeup.plist"
    plist_path.parent.mkdir(parents=True)
    plist_path.write_bytes(plistlib.dumps(wakeup_supervisor._plist(paths)))
    calls: list[tuple[str, ...]] = []
    loaded = True

    def launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
        nonlocal loaded
        calls.append(arguments)
        if arguments[0] == "bootout":
            loaded = False
        return completed(list(arguments), 0 if loaded or arguments[0] == "bootout" else 1)

    monkeypatch.setattr(wakeup_supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(wakeup_supervisor, "launch_agent_path", lambda: plist_path)
    monkeypatch.setattr(wakeup_supervisor, "_run_launchctl", launchctl)

    result = wakeup_supervisor.uninstall(paths)

    assert result["status"] == "uninstalled"
    assert not plist_path.exists()
    assert any(call[0] == "bootout" for call in calls)


def test_install_fails_explicitly_without_launchd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wakeup_supervisor.sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="requires macOS launchd"):
        wakeup_supervisor.install(wakeup.wakeup_paths(tmp_path / "state"))
