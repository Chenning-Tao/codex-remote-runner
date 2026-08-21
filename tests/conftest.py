from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _owned_test_group(runtime: Path) -> tuple[int, str] | None:
    try:
        owner = json.loads((runtime / "owner.json").read_text(encoding="utf-8"))
        run_id = owner["run_id"]
        pid = owner["pid"]
        pgid = owner["pgid"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError, OSError, TypeError):
        return None
    if (
        not isinstance(run_id, str)
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or isinstance(pgid, bool)
        or not isinstance(pgid, int)
        or pid != pgid
        or pgid <= 1
        or pgid == os.getpgrp()
    ):
        return None
    try:
        inspected = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "pid=,pgid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        try:
            return (pgid, run_id) if os.getpgid(pid) == pgid else None
        except (ProcessLookupError, PermissionError):
            return None
    fields = inspected.stdout.strip().split(None, 2)
    if len(fields) != 3:
        return None
    try:
        observed_pid = int(fields[0])
        observed_pgid = int(fields[1])
    except ValueError:
        return None
    command = fields[2]
    if (
        observed_pid != pid
        or observed_pgid != pgid
        or f"remote-runner:{run_id}" not in command
        or str(runtime) not in command
    ):
        return None
    return pgid, run_id


def _terminate_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1.0
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)
    if _group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
    deadline = time.monotonic() + 2.0
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.fixture
def reap_test_runner_processes(tmp_path: Path):
    yield
    for owner_path in tmp_path.rglob("owner.json"):
        runtime = owner_path.parent
        owned = _owned_test_group(runtime)
        if owned is None:
            continue
        pgid, run_id = owned
        _terminate_group(pgid)
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={run_id}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if _group_alive(pgid):
            pytest.fail(f"test left runner-owned process group alive: {pgid}")
