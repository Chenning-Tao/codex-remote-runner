from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Barrier, Event, Thread

import pytest

from remote_runner._internal.controller import dispatcher as controller_dispatcher
from remote_runner._internal.controller.registry import (
    acquire_dispatch_lease,
    controller_paths,
    controller_scheduler_paths,
    ensure_server_capacities,
    MalformedLeaseError,
    load_job,
    reserve_queued_job_update,
    set_server_drained,
    submit_job,
    transition_queued_state,
    update_queued_job,
    update_server_capacity,
)
from remote_runner._internal.execution_registry import load_yaml, sha256_bytes, write_yaml
from remote_runner._internal.scheduling import CapacityCandidate
from remote_runner._internal.worktree import WorktreeResult


RUN_ID = "rr-0123456789abcdef"


def test_server_probe_counts_only_live_runner_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / ".rr" / RUN_ID
    runtime.mkdir(parents=True)
    (runtime / "status.json").write_text(
        json.dumps({"run_id": RUN_ID, "label": "hardware test", "state": "running"}),
        encoding="utf-8",
    )
    (runtime / "pgid").write_text(str(os.getpgrp()), encoding="utf-8")

    live = subprocess.run(
        [sys.executable, "-"],
        input=controller_dispatcher.SERVER_STATE_PROBE_PROGRAM,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "HOME": str(tmp_path)},
        check=False,
    )
    assert live.returncode == 0, live.stderr
    live_payload = json.loads(live.stdout)
    assert live_payload["active_run_ids"] == [RUN_ID]
    assert live_payload["active_runs"] == [
        {
            "run_id": RUN_ID,
            "label": "hardware test",
            "workload_class": "standard",
        }
    ]

    (runtime / "pgid").unlink()
    stale = subprocess.run(
        [sys.executable, "-"],
        input=controller_dispatcher.SERVER_STATE_PROBE_PROGRAM,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "HOME": str(tmp_path)},
        check=False,
    )
    assert stale.returncode == 0, stale.stderr
    assert json.loads(stale.stdout)["active_run_ids"] == []


def test_server_probe_checks_an_exact_run_session(tmp_path: Path) -> None:
    runtime = tmp_path / ".rr" / RUN_ID
    runtime.mkdir(parents=True)
    (runtime / "status.json").write_text(
        json.dumps({"run_id": RUN_ID, "state": "running"}),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "tmux.calls"
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {str(call_log)!r}\nexit 1\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)

    completed = subprocess.run(
        [sys.executable, "-"],
        input=controller_dispatcher.SERVER_STATE_PROBE_PROGRAM,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        },
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert call_log.read_text(encoding="utf-8").splitlines() == [
        f"has-session -t ={RUN_ID}"
    ]


def test_capacity_probes_servers_concurrently_and_preserves_order(monkeypatch) -> None:
    started = Barrier(2)

    def probe(ssh: str, _python: str, _timeout: int) -> dict[str, object]:
        started.wait(timeout=1)
        return {
            "reachable": True,
            "load5": 1.0 if ssh == "compute-b" else 2.0,
            "active_run_ids": (),
        }

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)
    servers = job(two_servers=True)["prepared_servers"]
    assert isinstance(servers, list)

    reachable, failures = controller_dispatcher._probe_prepared_servers(servers, 8)

    assert failures == []
    assert [item.capacity.name for item in reachable] == ["compute-b", "archive"]
    assert [item.capacity.load5 for item in reachable] == [1.0, 2.0]


def test_dispatch_waits_for_live_lease_on_interrupted_head(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=0,
        status="dispatching",
    )
    assert acquire_dispatch_lease(
        paths,
        server="compute-b",
        run_id=RUN_ID,
        ttl_seconds=120,
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "busy"
    assert outcome.run_id == RUN_ID


def test_lease_heartbeat_covers_startup_beyond_initial_ttl(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "controller"
    paths = controller_paths(root, "project-a")
    queued = job()
    queued["lease_seconds"] = 1
    submit_job(paths, queued)
    entered = Event()
    proceed = Event()
    outcomes: list[controller_dispatcher.DispatchOutcome] = []

    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda *_args: {"reachable": True, "load5": 0.0, "active_run_ids": ()},
    )

    def prepare(**_kwargs) -> WorktreeResult:
        entered.set()
        assert proceed.wait(timeout=3)
        return WorktreeResult("/srv/example/worktrees/" + "a" * 40, False)

    monkeypatch.setattr(controller_dispatcher, "prepare_remote_worktree", prepare)
    monkeypatch.setattr(
        controller_dispatcher, "_register_execution", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    thread = Thread(target=lambda: outcomes.append(controller_dispatcher.dispatch_once(paths)))
    thread.start()
    assert entered.wait(timeout=2)
    lease_path = controller_scheduler_paths(root).leases_dir / "compute-b.yaml"
    deadline = time.monotonic() + 2
    lease = load_yaml(lease_path)
    while lease["heartbeat_at"] == lease["created_at"] and time.monotonic() < deadline:
        time.sleep(0.02)
        lease = load_yaml(lease_path)
    assert lease["heartbeat_at"] > lease["created_at"]
    assert lease["expires_at"] > lease["created_at"] + 1
    assert acquire_dispatch_lease(
        controller_paths(root, "project-b"),
        server="compute-b",
        run_id="rr-fedcba9876543210",
        ttl_seconds=1,
        now=float(lease["created_at"]) + 2,
    ) is None

    proceed.set()
    thread.join(timeout=3)
    assert outcomes[0].action == "started"


def test_lost_lease_owner_cannot_report_success(tmp_path: Path, monkeypatch) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    queued = job()
    queued["lease_seconds"] = 1
    submit_job(paths, queued)
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda *_args: {"reachable": True, "load5": 0.0, "active_run_ids": ()},
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult(
            "/srv/example/worktrees/" + "a" * 40,
            False,
        ),
    )
    monkeypatch.setattr(
        controller_dispatcher, "_register_execution", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())

    def steal_fence(*_args, **_kwargs) -> None:
        replacement = acquire_dispatch_lease(
            paths,
            server="compute-b",
            run_id=RUN_ID,
            ttl_seconds=1,
            now=time.time() + 10,
        )
        assert replacement is not None

    monkeypatch.setattr(controller_dispatcher.launch, "launch", steal_fence)

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "unknown"
    assert outcome.action != "started"


def test_lost_queue_commit_owner_cannot_report_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda *_args: {"reachable": True, "load5": 0.0, "active_run_ids": ()},
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult(
            "/srv/example/worktrees/" + "a" * 40,
            False,
        ),
    )
    monkeypatch.setattr(
        controller_dispatcher, "_register_execution", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())

    def lose_queue_commit(*_args, **_kwargs) -> None:
        transition_queued_state(
            paths,
            RUN_ID,
            expected_revision=1,
            status="failed",
            error="competing controller decision",
        )

    monkeypatch.setattr(controller_dispatcher.launch, "launch", lose_queue_commit)

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "unknown"
    assert outcome.action != "started"
    assert acquire_dispatch_lease(
        controller_paths(paths.root, "other-project"),
        server="compute-b",
        run_id="rr-fedcba9876543210",
        ttl_seconds=120,
        now=time.time() + 10_000,
    ) is None


def test_dispatcher_fails_closed_on_malformed_global_lease(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    scheduler = controller_scheduler_paths(paths.root)
    scheduler.leases_dir.mkdir(parents=True)
    (scheduler.leases_dir / "compute-b.yaml").write_text(
        "owner_token: [broken\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda *_args: pytest.fail("malformed lease must block before probing"),
    )

    with pytest.raises(MalformedLeaseError, match="malformed dispatch lease"):
        controller_dispatcher.dispatch_once(paths)


def test_queued_urgent_job_does_not_bypass_active_dispatch(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(), now="2026-01-01T00:00:00+00:00")
    transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=0,
        status="dispatching",
    )
    assert acquire_dispatch_lease(
        paths,
        server="compute-b",
        run_id=RUN_ID,
        ttl_seconds=120,
    )
    urgent = job(queue_priority="urgent")
    urgent["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, urgent, now="2026-01-01T00:00:01+00:00")

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "busy"
    assert outcome.run_id == RUN_ID


def test_dispatch_recovers_registered_execution_after_controller_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=0,
        status="dispatching",
    )
    write_yaml(paths.config_path, {"controller_registry": True})
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher, "registry_kind", lambda _paths, _run_id: "current"
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "idle"
    assert load_job(paths, RUN_ID)[1]["status"] == "dispatched"


def job(
    *,
    two_servers: bool = False,
    queue_priority: str = "normal",
    workload_class: str = "standard",
    command: str = "python experiment.py",
) -> dict[str, object]:
    servers = [
        {
            "name": "compute-b",
            "ssh": "compute-b",
            "ssh_profile": "intranet",
            "configured_cores": 256,
            "priority": 100,
            "bare_repo": "/srv/example/repo.git",
            "worktree_root": "/srv/example/worktrees",
            "python": "/opt/example/bin/python3",
            "output_root": "/home/a/project",
            "test_slots": 1 if workload_class == "test" else 0,
        }
    ]
    if two_servers:
        servers.append(
            {
                "name": "archive",
                "ssh": "archive",
                "ssh_profile": "tailscale",
                "configured_cores": 32,
                "priority": 100,
                "bare_repo": "/srv/example/repo.git",
                "worktree_root": "/srv/example/worktrees",
                "python": "/opt/example/bin/python3",
                "output_root": "/home/b/project",
                "test_slots": 1 if workload_class == "test" else 0,
            }
        )
    return {
        "run_id": RUN_ID,
        "revision": "a" * 40,
        "label": "experiment",
        "task_id": "task-1",
        "queue_priority": queue_priority,
        "workload_class": workload_class,
        "submitted_command": command,
        "submitted_command_sha256": sha256_bytes(command.encode()),
        "prepared_servers": servers,
        "output_relpath": None,
        "output_path": None,
        "output_metadata": {},
        "lease_seconds": 120,
    }


def test_execution_registration_preserves_minimum_cores(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    queued = job()
    queued["minimum_cores"] = 256
    server = queued["prepared_servers"][0]
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        controller_dispatcher.registration,
        "register",
        lambda args: captured.update(vars(args)),
    )
    controller_dispatcher._register_execution(
        paths,
        queued,
        server,
        workdir="/srv/example/worktrees/" + "a" * 40,
        assigned_cores=256,
        output_root=None,
        output_relpath=None,
        output_path=None,
    )

    assert captured["minimum_cores"] == 256
    assert captured["configured_cores"] == 256
    assert captured["assigned_cores"] == 256
    assert captured["command"] == "python experiment.py"


def test_dispatch_selects_urgent_job_ahead_of_older_normal_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(), now="2026-01-01T00:00:00+00:00")
    urgent = job(queue_priority="urgent")
    urgent["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, urgent, now="2026-01-01T00:00:01+00:00")
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda _ssh, _python, _timeout: {
            "reachable": True,
            "load5": 0.0,
            "active_run_ids": (),
        },
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult(
            "/srv/example/worktrees/" + "a" * 40,
            False,
        ),
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "_register_execution",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch,
        "launch",
        lambda *_args, **_kwargs: None,
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "started"
    assert outcome.run_id == "rr-fedcba9876543210"
    assert load_job(paths, RUN_ID)[1]["status"] == "queued"


def test_dispatch_skips_job_while_server_preparation_is_reserved(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(), now="2026-01-01T00:00:00+00:00")
    reserve_queued_job_update(
        paths,
        RUN_ID,
        expected_revision=0,
        requested_servers=["compute-b"],
        ttl_seconds=60,
    )
    second = job()
    second["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, second, now="2026-01-01T00:00:01+00:00")
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda _ssh, _python, _timeout: {
            "reachable": True,
            "load5": 0.0,
            "active_run_ids": (),
        },
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult(
            "/srv/example/worktrees/" + "a" * 40,
            False,
        ),
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "_register_execution",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch,
        "launch",
        lambda *_args, **_kwargs: None,
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "started"
    assert outcome.run_id == "rr-fedcba9876543210"
    assert load_job(paths, RUN_ID)[1]["status"] == "queued"


def test_saturated_runner_owned_work_stays_queued(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda _ssh, _python, _timeout: {
            "reachable": True,
            "load5": 300.0,
            "active_run_ids": ("rr-running00000000",),
        },
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "queued"
    assert outcome.message == "standard slots full (1/1)"


def test_dispatch_does_not_probe_server_in_frozen_snapshot_after_drain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    set_server_drained(paths, "compute-b", drained=True)
    probed: list[str] = []
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda ssh, _python, _timeout: probed.append(ssh),
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "queued"
    assert outcome.message == "all prepared servers are drained"
    assert probed == []


def test_recent_runner_work_stays_queued_before_load5_rises(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda _ssh, _python, _timeout: {
            "reachable": True,
            "load5": 0.0,
            "active_run_ids": ("rr-running00000000",),
        },
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "queued"
    assert outcome.message == "standard slots full (1/1)"


def test_external_saturation_without_runner_work_still_dispatches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda _ssh, _python, _timeout: {
            "reachable": True,
            "load5": 300.0,
            "active_run_ids": (),
        },
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult("/srv/example/worktrees/" + "a" * 40, False),
    )
    recorded: dict[str, object] = {}
    monkeypatch.setattr(
        controller_dispatcher,
        "_register_execution",
        lambda _paths, _job, server, **kwargs: recorded.update(server=server, **kwargs),
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "started"
    assert outcome.server == "compute-b"
    assert recorded["assigned_cores"] == 256


def test_dispatch_prefers_absolute_headroom(tmp_path: Path, monkeypatch) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(two_servers=True))

    def probe(ssh: str, _python: str, _timeout: int) -> dict[str, object]:
        return {
            "reachable": True,
            "load5": 128.0 if ssh == "compute-b" else 0.0,
            "active_run_ids": (),
        }

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult("/srv/example/worktrees/" + "a" * 40, False),
    )
    selected: dict[str, object] = {}
    monkeypatch.setattr(
        controller_dispatcher,
        "_register_execution",
        lambda _paths, _job, server, **kwargs: selected.update(server=server, **kwargs),
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.server == "compute-b"
    assert selected["assigned_cores"] == 256


def test_dispatch_only_probes_manually_enabled_servers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(two_servers=True))
    update_queued_job(
        paths,
        RUN_ID,
        expected_revision=0,
        eligible_servers=["archive"],
    )
    probed: list[str] = []

    def probe(ssh: str, _python: str, _timeout: int) -> dict[str, object]:
        probed.append(ssh)
        return {"reachable": True, "load5": 0.0, "active_run_ids": ()}

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult(
            "/srv/example/worktrees/" + "a" * 40,
            False,
        ),
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "_register_execution",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "started"
    assert outcome.server == "archive"
    assert set(probed) == {"archive"}


def test_dispatch_avoids_active_server_before_load5_rises(
    tmp_path: Path, monkeypatch
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    queued = job(two_servers=True)
    queued["output_relpath"] = "validation/result.json"
    submit_job(paths, queued)

    def probe(ssh: str, _python: str, _timeout: int) -> dict[str, object]:
        return {
            "reachable": True,
            "load5": 0.0,
            "active_run_ids": ("rr-running00000000",) if ssh == "compute-b" else (),
        }

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult("/srv/example/worktrees/" + "a" * 40, False),
    )
    selected: dict[str, object] = {}
    monkeypatch.setattr(
        controller_dispatcher,
        "_register_execution",
        lambda _paths, _job, server, **kwargs: selected.update(
            server=server,
            **kwargs,
        ),
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "started"
    assert outcome.server == "archive"
    assert selected["output_root"] == "/home/b/project"
    assert selected["output_relpath"] == "validation/result.json"
    assert selected["output_path"] == "/home/b/project/validation/result.json"


def test_blocked_standard_head_does_not_hide_test_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(), now="2026-01-01T00:00:00+00:00")
    standard_backfill = job(two_servers=True)
    standard_backfill["run_id"] = "rr-1111111111111111"
    standard_backfill["prepared_servers"] = standard_backfill["prepared_servers"][1:]
    submit_job(paths, standard_backfill, now="2026-01-01T00:00:00.500000+00:00")
    test_job = job(
        workload_class="test",
        command="python -m pytest tests/test_scheduler.py -q",
    )
    test_job["requested_cores"] = 16
    test_job["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, test_job, now="2026-01-01T00:00:01+00:00")
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda ssh, *_args: {
            "reachable": True,
            "load5": 256.0 if ssh == "compute-b" else 0.0,
            "active_runs": (
                (
                    {
                        "run_id": "rr-running00000000",
                        "workload_class": "standard",
                        "assigned_cores": 240,
                    },
                )
                if ssh == "compute-b"
                else ()
            ),
        },
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult("/srv/example/worktrees/" + "a" * 40, False),
    )
    selected: dict[str, object] = {}
    monkeypatch.setattr(
        controller_dispatcher,
        "_register_execution",
        lambda _paths, queued, _server, **kwargs: selected.update(job=queued, **kwargs),
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "started"
    assert outcome.run_id == "rr-fedcba9876543210"
    assert selected["assigned_cores"] == 16
    assert selected["job"]["submitted_command"] == "python -m pytest tests/test_scheduler.py -q"
    assert load_job(paths, RUN_ID)[1]["status"] == "queued"
    assert load_job(paths, str(standard_backfill["run_id"]))[1]["status"] == "queued"


def test_blocked_test_head_backfills_on_unreserved_server(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(
        paths,
        job(workload_class="test"),
        now="2026-01-01T00:00:00+00:00",
    )
    backfill = job(two_servers=True, workload_class="test")
    backfill["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, backfill, now="2026-01-01T00:00:01+00:00")

    def probe(ssh: str, *_args) -> dict[str, object]:
        active = ()
        if ssh == "compute-b":
            active = ({"run_id": "rr-testing00000000", "workload_class": "test"},)
        return {"reachable": True, "load5": 0.0, "active_runs": active}

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult("/srv/example/worktrees/" + "a" * 40, False),
    )
    selected: dict[str, object] = {}
    monkeypatch.setattr(
        controller_dispatcher,
        "_register_execution",
        lambda _paths, queued, server, **_kwargs: selected.update(
            run_id=queued["run_id"],
            server_name=server["name"],
        ),
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "started"
    assert outcome.run_id == "rr-fedcba9876543210"
    assert outcome.server == "archive"
    assert selected["run_id"] == backfill["run_id"]
    assert selected["server_name"] == "archive"
    assert load_job(paths, RUN_ID)[1]["status"] == "queued"


def test_blocked_test_head_keeps_its_servers_protected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(
        paths,
        job(workload_class="test"),
        now="2026-01-01T00:00:00+00:00",
    )
    later = job(workload_class="test")
    later["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, later, now="2026-01-01T00:00:01+00:00")
    probes: list[str] = []

    def probe(ssh: str, *_args) -> dict[str, object]:
        probes.append(ssh)
        return {
            "reachable": True,
            "load5": 0.0,
            "active_runs": (
                {"run_id": "rr-testing00000000", "workload_class": "test"},
            ),
        }

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "queued"
    assert outcome.run_id == RUN_ID
    assert outcome.message == "test slots full (1/1)"
    assert probes == ["compute-b"]
    assert load_job(paths, str(later["run_id"]))[1]["status"] == "queued"


def test_blocked_urgent_job_backfills_normal_on_unreserved_server(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(
        paths,
        job(queue_priority="urgent"),
        now="2026-01-01T00:00:00+00:00",
    )
    normal = job(two_servers=True)
    normal["run_id"] = "rr-fedcba9876543210"
    prepared_servers = normal["prepared_servers"]
    assert isinstance(prepared_servers, list)
    normal["prepared_servers"] = prepared_servers[1:]
    submit_job(paths, normal, now="2026-01-01T00:00:01+00:00")

    def probe(ssh: str, *_args) -> dict[str, object]:
        active = ()
        if ssh == "compute-b":
            active = ({"run_id": "rr-running00000000", "workload_class": "standard"},)
        return {
            "reachable": True,
            "load5": 0.0,
            "active_runs": active,
        }

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult("/srv/example/worktrees/" + "a" * 40, False),
    )
    selected: dict[str, object] = {}
    monkeypatch.setattr(
        controller_dispatcher,
        "_register_execution",
        lambda _paths, queued, server, **_kwargs: selected.update(
            run_id=queued["run_id"],
            server_name=server["name"],
        ),
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "started"
    assert outcome.run_id == normal["run_id"]
    assert outcome.server == "archive"
    assert selected == {"run_id": normal["run_id"], "server_name": "archive"}
    assert load_job(paths, RUN_ID)[1]["status"] == "queued"


def test_blocked_urgent_job_protects_server_from_normal_priority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(
        paths,
        job(queue_priority="urgent"),
        now="2026-01-01T00:00:00+00:00",
    )
    normal = job()
    normal["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, normal, now="2026-01-01T00:00:01+00:00")
    probes: list[str] = []

    def probe(ssh: str, *_args) -> dict[str, object]:
        probes.append(ssh)
        return {
            "reachable": True,
            "load5": 0.0,
            "active_runs": (
                {"run_id": "rr-running00000000", "workload_class": "standard"},
            ),
        }

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "queued"
    assert outcome.run_id == RUN_ID
    assert probes == ["compute-b"]
    assert load_job(paths, str(normal["run_id"]))[1]["status"] == "queued"


def test_test_lane_waits_when_its_server_slots_are_full(
    tmp_path: Path, monkeypatch
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(workload_class="test"))
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda *_args: {
            "reachable": True,
            "load5": 512.0,
            "active_runs": (
                {"run_id": "rr-running00000000", "workload_class": "standard"},
                {"run_id": "rr-testing00000000", "workload_class": "test"},
            ),
        },
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "queued"
    assert outcome.message == "test slots full (1/1)"


def test_test_slot_is_rechecked_after_global_lease(tmp_path: Path, monkeypatch) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(workload_class="test"))
    probes = 0

    def probe(*_args) -> dict[str, object]:
        nonlocal probes
        probes += 1
        active = ()
        if probes == 2:
            active = ({"run_id": "rr-testing00000000", "workload_class": "test"},)
        return {"reachable": True, "load5": 0.0, "active_runs": active}

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)

    outcome = controller_dispatcher.dispatch_once(paths)

    assert probes == 2
    assert outcome.action == "queued"
    assert outcome.message == "test slots full (1/1)"


def test_test_pool_uses_another_server_when_one_server_is_full(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(two_servers=True, workload_class="test"))

    def probe(ssh: str, *_args) -> dict[str, object]:
        active = ()
        if ssh == "compute-b":
            active = ({"run_id": "rr-testing00000000", "workload_class": "test"},)
        return {"reachable": True, "load5": 0.0, "active_runs": active}

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult("/srv/example/worktrees/" + "a" * 40, False),
    )
    monkeypatch.setattr(
        controller_dispatcher, "_register_execution", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "started"
    assert outcome.server == "archive"


def test_standard_lane_shares_remaining_cores_with_running_test_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    queued = job()
    queued["requested_cores"] = 8
    submit_job(paths, queued)
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda *_args: {
            "reachable": True,
            "load5": 256.0,
            "active_runs": (
                {
                    "run_id": "rr-testing00000000",
                    "workload_class": "test",
                    "assigned_cores": 8,
                },
            ),
        },
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult("/srv/example/worktrees/" + "a" * 40, False),
    )
    monkeypatch.setattr(
        controller_dispatcher, "_register_execution", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcomes = controller_dispatcher.dispatch_batch(paths)

    assert outcomes[0].action == "started"
    assert outcomes[0].run_id == RUN_ID


def test_large_machine_core_budget_blocks_cross_lane_overcommit() -> None:
    candidate = controller_dispatcher.ProbedServer(
        server={
            "name": "epyc",
            "machine_id": "epyc-9965",
            "configured_cores": 768,
            "standard_slots": 4,
            "test_slots": 4,
        },
        capacity=CapacityCandidate(
            name="epyc",
            configured_cores=768,
            load5=0.0,
            allocated_cores=700,
            active_run_count=2,
        ),
        active_standard_count=1,
        active_test_count=1,
        active_run_ids=("rr-a", "rr-b"),
        active_assigned_cores=700,
    )

    assert not controller_dispatcher._has_workload_capacity(
        {"workload_class": "test", "requested_cores": 100},
        candidate,
    )
    assert controller_dispatcher._has_workload_capacity(
        {"workload_class": "standard", "requested_cores": 68},
        candidate,
    )


@pytest.mark.parametrize(
    ("workload_class", "capacity_changes", "active_class"),
    [
        ("standard", {"standard_slots": 2, "test_slots": 1}, "standard"),
        ("test", {"standard_slots": 1, "test_slots": 2}, "test"),
    ],
)
def test_dispatch_uses_live_controller_slots_for_existing_queued_job(
    tmp_path: Path,
    monkeypatch,
    workload_class: str,
    capacity_changes: dict[str, int],
    active_class: str,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    queued = job(workload_class=workload_class)
    queued["requested_cores"] = 8
    submit_job(paths, queued)
    server = queued["prepared_servers"][0]
    ensure_server_capacities(paths, [server])
    update_server_capacity(
        paths,
        str(server["name"]),
        expected_revision=0,
        **capacity_changes,
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda *_args: {
            "reachable": True,
            "load5": 0.0,
            "active_runs": (
                {
                    "run_id": "rr-running00000000",
                    "workload_class": active_class,
                    "assigned_cores": 8,
                },
            ),
        },
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult("/srv/example/worktrees/" + "a" * 40, False),
    )
    monkeypatch.setattr(
        controller_dispatcher, "_register_execution", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcomes = controller_dispatcher.dispatch_batch(paths)

    assert outcomes[0].action == "started"
    assert outcomes[0].run_id == RUN_ID


def test_unknown_launch_outcome_stays_reconcilable(tmp_path: Path, monkeypatch) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    monkeypatch.setattr(
        controller_dispatcher,
        "probe_server_state",
        lambda _ssh, _python, _timeout: {
            "reachable": True,
            "load5": 0.0,
            "active_run_ids": (),
        },
    )
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **_kwargs: WorktreeResult("/srv/example/worktrees/" + "a" * 40, False),
    )
    monkeypatch.setattr(
        controller_dispatcher, "_register_execution", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())

    def unknown(*_args, **_kwargs) -> None:
        try:
            raise controller_dispatcher.launch.BootstrapOutcomeUnknown(
                "connection dropped"
            )
        except controller_dispatcher.launch.BootstrapOutcomeUnknown as exc:
            raise RuntimeError(str(exc)) from exc

    monkeypatch.setattr(controller_dispatcher.launch, "launch", unknown)

    outcome = controller_dispatcher.dispatch_once(paths)

    assert outcome.action == "unknown"
    assert load_job(paths, RUN_ID)[1]["status"] == "dispatched"
    assert acquire_dispatch_lease(
        controller_paths(paths.root, "other-project"),
        server="compute-b",
        run_id="rr-fedcba9876543210",
        ttl_seconds=120,
        now=time.time() + 10_000,
    ) is None


def test_dispatch_batch_shares_probes_and_launches_distinct_servers_concurrently(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(two_servers=True), now="2026-01-01T00:00:00+00:00")
    second = job(two_servers=True)
    second["run_id"] = "rr-fedcba9876543210"
    second["output_relpath"] = "second/result.json"
    second_servers = second["prepared_servers"]
    assert isinstance(second_servers, list)
    second_servers[1]["output_root"] = "/home/second/project"
    submit_job(paths, second, now="2026-01-01T00:00:01+00:00")
    probes: list[str] = []
    registrations: dict[str, dict[str, object]] = {}

    def probe(ssh: str, *_args) -> dict[str, object]:
        probes.append(ssh)
        return {"reachable": True, "load5": 0.0, "active_run_ids": ()}

    launches = Barrier(2)

    def prepare(**kwargs) -> WorktreeResult:
        launches.wait(timeout=2)
        return WorktreeResult(
            "/srv/example/worktrees/" + str(kwargs["revision"]),
            False,
        )

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)
    monkeypatch.setattr(controller_dispatcher, "prepare_remote_worktree", prepare)

    def register(_paths, queued, server, **kwargs) -> None:
        registrations[str(queued["run_id"])] = {"server": server, **kwargs}

    monkeypatch.setattr(controller_dispatcher, "_register_execution", register)
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcomes = controller_dispatcher.dispatch_batch(paths)

    assert [outcome.action for outcome in outcomes] == ["started", "started"]
    assert {outcome.server for outcome in outcomes} == {"compute-b", "archive"}
    assert probes.count("compute-b") == 2
    assert probes.count("archive") == 2
    assert registrations[str(second["run_id"])]["output_root"] == "/home/second/project"
    assert registrations[str(second["run_id"])]["output_path"] == (
        "/home/second/project/second/result.json"
    )
    assert load_job(paths, RUN_ID)[1]["status"] == "dispatched"
    assert load_job(paths, str(second["run_id"]))[1]["status"] == "dispatched"


def test_dispatch_batch_rechecks_each_server_after_global_lease(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(two_servers=True), now="2026-01-01T00:00:00+00:00")
    second = job(two_servers=True)
    second["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, second, now="2026-01-01T00:00:01+00:00")
    probes: dict[str, int] = {"compute-b": 0, "archive": 0}

    def probe(ssh: str, *_args) -> dict[str, object]:
        probes[ssh] += 1
        active = ()
        if ssh == "archive" and probes[ssh] == 2:
            active = ({"run_id": "rr-external0000000", "workload_class": "standard"},)
        return {"reachable": True, "load5": 0.0, "active_runs": active}

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **kwargs: WorktreeResult(
            "/srv/example/worktrees/" + str(kwargs["revision"]), False
        ),
    )
    monkeypatch.setattr(
        controller_dispatcher, "_register_execution", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcomes = controller_dispatcher.dispatch_batch(paths)

    assert sorted(outcome.action for outcome in outcomes) == ["queued", "started"]
    assert probes == {"compute-b": 2, "archive": 2}
    assert load_job(paths, RUN_ID)[1]["status"] == "dispatched"
    assert load_job(paths, str(second["run_id"]))[1]["status"] == "queued"


def test_dispatch_batch_tries_unplanned_server_when_preferred_lease_is_busy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job(two_servers=True))
    assert acquire_dispatch_lease(
        paths,
        server="compute-b",
        run_id="rr-1111111111111111",
        ttl_seconds=120,
    )
    probes: list[str] = []

    def probe(ssh: str, *_args) -> dict[str, object]:
        probes.append(ssh)
        return {"reachable": True, "load5": 0.0, "active_run_ids": ()}

    monkeypatch.setattr(controller_dispatcher, "probe_server_state", probe)
    monkeypatch.setattr(
        controller_dispatcher,
        "prepare_remote_worktree",
        lambda **kwargs: WorktreeResult(
            "/srv/example/worktrees/" + str(kwargs["revision"]), False
        ),
    )
    monkeypatch.setattr(
        controller_dispatcher, "_register_execution", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(controller_dispatcher, "project_paths", lambda _path: object())
    monkeypatch.setattr(
        controller_dispatcher.launch, "launch", lambda *_args, **_kwargs: None
    )

    outcomes = controller_dispatcher.dispatch_batch(paths)

    assert len(outcomes) == 1
    assert outcomes[0].action == "started"
    assert outcomes[0].server == "archive"
    assert probes.count("compute-b") == 1
    assert probes.count("archive") == 2


def test_dispatch_loop_immediately_drains_started_batches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    batches = iter(
        (
            [
                controller_dispatcher.DispatchOutcome("started", RUN_ID, "compute-b"),
                controller_dispatcher.DispatchOutcome(
                    "started", "rr-fedcba9876543210", "archive"
                ),
            ],
            [controller_dispatcher.DispatchOutcome("queued", RUN_ID)],
        )
    )
    dispatches: list[controller_dispatcher.DispatchOutcome] = []

    def dispatch_batch(
        *_args, **_kwargs
    ) -> list[controller_dispatcher.DispatchOutcome]:
        outcomes = next(batches)
        dispatches.extend(outcomes)
        return outcomes

    monkeypatch.setattr(controller_dispatcher, "dispatch_batch", dispatch_batch)

    def stop_after_first_sleep(_seconds: int) -> None:
        raise RuntimeError("stop test loop")

    monkeypatch.setattr(controller_dispatcher.time, "sleep", stop_after_first_sleep)

    with pytest.raises(RuntimeError, match="stop test loop"):
        controller_dispatcher.dispatch_loop(paths, interval_seconds=17)

    assert [outcome.action for outcome in dispatches] == [
        "started",
        "started",
        "queued",
    ]
