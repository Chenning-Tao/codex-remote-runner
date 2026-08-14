from __future__ import annotations

import stat
from pathlib import Path

import pytest

from remote_runner._internal.controller.registry import (
    acquire_dispatch_lease,
    acquire_maintenance_lease,
    controller_paths,
    controller_scheduler_paths,
    ensure_server_capacities,
    MalformedLeaseError,
    has_unexpired_dispatch_lease,
    list_drained_servers,
    list_jobs,
    list_queued,
    list_queued_all,
    list_server_capacities,
    load_job,
    recover_dispatching_state,
    renew_dispatch_lease,
    release_queued_job_update,
    release_dispatch_lease,
    extend_queued_all,
    extend_queued_job,
    submit_job,
    placement_update_active,
    reserve_queued_job_update,
    set_server_drained,
    transition_queued_state,
    update_queued_job,
    update_server_capacity,
)
from remote_runner._internal.execution_registry import (
    load_yaml,
    sha256_bytes,
    write_yaml,
)


RUN_ID = "rr-0123456789abcdef"


def queued_job() -> dict[str, object]:
    command = "python experiment.py"
    return {
        "run_id": RUN_ID,
        "revision": "a" * 40,
        "label": "experiment",
        "task_id": "task-1",
        "submitted_command": command,
        "submitted_command_sha256": sha256_bytes(command.encode()),
        "prepared_servers": [
            {
                "name": "compute-a",
                "ssh": "compute-a",
                "ssh_profile": "intranet",
                "configured_cores": 256,
                "priority": 100,
                "bare_repo": "/srv/example/repo.git",
                "worktree_root": "/srv/example/worktrees",
                "python": "/opt/example/bin/python3",
                "output_root": None,
            }
        ],
        "output_relpath": None,
        "output_path": "/srv/example/exp/result.json",
        "output_metadata": {},
    }


def test_submit_and_list_fifo_jobs_privately(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job(), now="2026-01-01T00:00:00+00:00")
    second = queued_job()
    second["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, second, now="2026-01-01T00:00:01+00:00")

    rows = list_queued(paths)
    assert [row[0]["run_id"] for row in rows] == [RUN_ID, "rr-fedcba9876543210"]
    job, state = load_job(paths, RUN_ID)
    assert job["revision"] == "a" * 40
    assert job["queue_priority"] == "normal"
    assert job["workload_class"] == "standard"
    assert state["status"] == "queued"
    assert stat.S_IMODE(paths.project_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((paths.queue_dir / RUN_ID / "job.yaml").stat().st_mode) == 0o600


def test_all_server_job_can_be_extended_while_queued(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    dynamic = queued_job()
    dynamic["server_scope"] = "all"
    dynamic["output_path"] = None
    submit_job(paths, dynamic)
    addition = {
        **dynamic["prepared_servers"][0],
        "name": "archive",
        "ssh": "archive",
        "ssh_profile": "tailscale",
        "configured_cores": 128,
        "priority": 20,
        "bare_repo": "/srv/archive/repo.git",
        "worktree_root": "/srv/archive/worktrees",
        "python": "/opt/archive/python3",
    }

    results = extend_queued_all(
        paths,
        [
            {
                "run_id": RUN_ID,
                "revision": "a" * 40,
                "prepared_servers": [addition],
            }
        ],
    )

    assert results == [{"run_id": RUN_ID, "status": "extended", "added_servers": 1}]
    assert list_queued_all(paths)[0]["prepared_servers"] == ["compute-a", "archive"]
    loaded = load_job(paths, RUN_ID)[0]
    assert [server["name"] for server in loaded["prepared_servers"]] == [
        "compute-a",
        "archive",
    ]
    assert loaded["eligible_servers"] == ["compute-a", "archive"]


def test_pool_extension_skips_snapshot_and_nonqueued_jobs(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())
    update = {
        "run_id": RUN_ID,
        "revision": "a" * 40,
        "prepared_servers": [],
    }
    assert extend_queued_all(paths, [update]) == [
        {"run_id": RUN_ID, "status": "skipped", "reason": "job changed"}
    ]

    dynamic = queued_job()
    dynamic["run_id"] = "rr-fedcba9876543210"
    dynamic["server_scope"] = "all"
    dynamic["output_path"] = None
    submit_job(paths, dynamic)
    transition_queued_state(
        paths,
        "rr-fedcba9876543210",
        expected_revision=0,
        status="stopped",
    )
    update["run_id"] = "rr-fedcba9876543210"
    assert extend_queued_all(paths, [update]) == [
        {"run_id": "rr-fedcba9876543210", "status": "skipped", "reason": "stopped"}
    ]


def test_one_queued_snapshot_can_be_extended_idempotently(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    snapshot = queued_job()
    snapshot["output_path"] = None
    submit_job(paths, snapshot)
    addition = {
        **snapshot["prepared_servers"][0],
        "name": "archive",
        "ssh": "archive",
        "configured_cores": 128,
    }

    result = extend_queued_job(
        paths,
        RUN_ID,
        revision="a" * 40,
        prepared_servers=[addition],
    )

    assert result == {
        "run_id": RUN_ID,
        "status": "extended",
        "added_servers": 1,
        "prepared_servers": ["compute-a", "archive"],
    }
    assert extend_queued_job(
        paths,
        RUN_ID,
        revision="a" * 40,
        prepared_servers=[addition],
    ) == {
        "run_id": RUN_ID,
        "status": "unchanged",
        "added_servers": 0,
        "prepared_servers": ["compute-a", "archive"],
    }


def test_one_job_extension_rejects_nonqueued_state(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    snapshot = queued_job()
    snapshot["output_path"] = None
    submit_job(paths, snapshot)
    transition_queued_state(paths, RUN_ID, expected_revision=0, status="dispatching")

    with pytest.raises(ValueError, match="is dispatching, not eligible"):
        extend_queued_job(
            paths,
            RUN_ID,
            revision="a" * 40,
            prepared_servers=[snapshot["prepared_servers"][0]],
        )


def test_urgent_jobs_precede_normal_jobs_and_remain_fifo(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job(), now="2026-01-01T00:00:00+00:00")
    first_urgent = queued_job()
    first_urgent["run_id"] = "rr-1111111111111111"
    first_urgent["queue_priority"] = "urgent"
    submit_job(paths, first_urgent, now="2026-01-01T00:00:01+00:00")
    second_urgent = queued_job()
    second_urgent["run_id"] = "rr-2222222222222222"
    second_urgent["queue_priority"] = "urgent"
    submit_job(paths, second_urgent, now="2026-01-01T00:00:02+00:00")

    rows = list_queued(paths)

    assert [row[0]["run_id"] for row in rows] == [
        "rr-1111111111111111",
        "rr-2222222222222222",
        RUN_ID,
    ]


def test_queued_jobs_can_be_reordered_within_their_scheduling_lane(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    run_ids = [RUN_ID, "rr-1111111111111111", "rr-2222222222222222"]
    for index, run_id in enumerate(run_ids):
        item = queued_job()
        item["run_id"] = run_id
        submit_job(paths, item, now=f"2026-01-01T00:00:0{index}+00:00")

    result = update_queued_job(
        paths,
        "rr-2222222222222222",
        expected_revision=0,
        move="up",
    )

    assert result["changed"] is True
    assert [job["run_id"] for job, _state in list_queued(paths)] == [
        RUN_ID,
        "rr-2222222222222222",
        "rr-1111111111111111",
    ]
    assert [load_job(paths, run_id)[1]["revision"] for run_id in run_ids] == [
        1,
        1,
        1,
    ]


def test_queued_job_can_be_moved_to_the_front_of_its_scheduling_lane(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    run_ids = [
        RUN_ID,
        "rr-1111111111111111",
        "rr-2222222222222222",
        "rr-3333333333333333",
    ]
    for index, run_id in enumerate(run_ids):
        item = queued_job()
        item["run_id"] = run_id
        submit_job(paths, item, now=f"2026-01-01T00:00:0{index}+00:00")

    result = update_queued_job(
        paths,
        "rr-3333333333333333",
        expected_revision=0,
        move="first",
    )

    assert result["changed"] is True
    assert [job["run_id"] for job, _state in list_queued(paths)] == [
        "rr-3333333333333333",
        RUN_ID,
        "rr-1111111111111111",
        "rr-2222222222222222",
    ]
    assert [load_job(paths, run_id)[1]["revision"] for run_id in run_ids] == [
        1,
        1,
        1,
        1,
    ]

    unchanged = update_queued_job(
        paths,
        "rr-3333333333333333",
        expected_revision=1,
        move="first",
    )

    assert unchanged["changed"] is False


def test_queued_job_priority_and_eligible_servers_can_be_changed(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    item = queued_job()
    item["output_path"] = None
    item["prepared_servers"].append(
        {
            **item["prepared_servers"][0],
            "name": "compute-b",
            "ssh": "compute-b",
        }
    )
    submit_job(paths, item)

    result = update_queued_job(
        paths,
        RUN_ID,
        expected_revision=0,
        queue_priority="urgent",
        eligible_servers=["compute-b"],
    )

    assert result["job"]["queue_priority"] == "urgent"
    assert result["job"]["eligible_servers"] == ["compute-b"]
    assert result["state"]["revision"] == 1
    with pytest.raises(RuntimeError, match="revision conflict"):
        update_queued_job(
            paths,
            RUN_ID,
            expected_revision=0,
            queue_priority="normal",
        )
    with pytest.raises(ValueError, match="unprepared server"):
        update_queued_job(
            paths,
            RUN_ID,
            expected_revision=1,
            eligible_servers=["unknown"],
        )


def test_queue_update_reservation_guards_preparation_and_commit(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    item = queued_job()
    item["output_path"] = None
    submit_job(paths, item)

    reservation = reserve_queued_job_update(
        paths,
        RUN_ID,
        expected_revision=0,
        requested_servers=["compute-a", "compute-b"],
        ttl_seconds=60,
    )

    token = reservation["token"]
    expires_at = reservation["state"]["placement_update"]["expires_at"]
    assert placement_update_active(reservation["state"], now=expires_at - 1) is True
    assert placement_update_active(reservation["state"], now=expires_at + 1) is False
    with pytest.raises(RuntimeError, match="placement update in progress"):
        update_queued_job(
            paths,
            RUN_ID,
            expected_revision=1,
            queue_priority="urgent",
        )

    prepared = {
        **item["prepared_servers"][0],
        "name": "compute-b",
        "ssh": "compute-b",
    }
    extended = extend_queued_job(
        paths,
        RUN_ID,
        revision="a" * 40,
        prepared_servers=[prepared],
        placement_token=token,
    )
    assert extended["prepared_servers"] == ["compute-a", "compute-b"]

    committed = update_queued_job(
        paths,
        RUN_ID,
        expected_revision=1,
        queue_priority="urgent",
        eligible_servers=["compute-b"],
        placement_token=token,
    )
    assert committed["job"]["eligible_servers"] == ["compute-b"]
    assert committed["state"]["revision"] == 2
    assert "placement_update" not in committed["state"]


def test_queue_update_reservation_can_be_released(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())
    reservation = reserve_queued_job_update(
        paths,
        RUN_ID,
        expected_revision=0,
        requested_servers=["compute-a"],
        ttl_seconds=60,
    )

    released = release_queued_job_update(
        paths,
        RUN_ID,
        token=reservation["token"],
    )

    assert released["changed"] is True
    assert released["state"]["revision"] == 2
    assert "placement_update" not in released["state"]


def test_invalid_queue_priority_is_rejected(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    invalid = queued_job()
    invalid["queue_priority"] = "critical"

    with pytest.raises(ValueError, match="queue priority must be one of"):
        submit_job(paths, invalid)


def test_test_workload_capacity_is_not_frozen_into_queue_validation(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    item = queued_job()
    item["workload_class"] = "test"
    submit_job(paths, item)

    assert load_job(paths, RUN_ID)[0]["workload_class"] == "test"


def test_server_capacity_defaults_refresh_until_customized_and_share_root(
    tmp_path: Path,
) -> None:
    first = controller_paths(tmp_path / "controller", "project-a")
    second = controller_paths(tmp_path / "controller", "project-b")
    initial = ensure_server_capacities(
        first,
        [{"name": "compute-a", "standard_slots": 1, "test_slots": 1}],
    )
    assert initial["compute-a"]["revision"] == 0
    refreshed = ensure_server_capacities(
        second,
        [{"name": "compute-a", "standard_slots": 1, "test_slots": 2}],
    )
    assert refreshed["compute-a"]["test_slots"] == 2
    assert refreshed["compute-a"]["revision"] == 1

    updated = update_server_capacity(
        first,
        "compute-a",
        expected_revision=1,
        standard_slots=3,
        test_slots=4,
    )
    ensure_server_capacities(
        second,
        [{"name": "compute-a", "standard_slots": 1, "test_slots": 1}],
    )
    assert list_server_capacities(second)["compute-a"] == updated["capacity"]
    with pytest.raises(RuntimeError, match="revision conflict"):
        update_server_capacity(
            second,
            "compute-a",
            expected_revision=1,
            standard_slots=1,
            test_slots=1,
        )


def test_queued_job_can_switch_workload_class_and_moves_to_destination_tail(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job(), now="2026-01-01T00:00:00+00:00")
    existing_test = queued_job()
    existing_test["run_id"] = "rr-fedcba9876543210"
    existing_test["workload_class"] = "test"
    submit_job(paths, existing_test, now="2026-01-01T00:00:01+00:00")

    result = update_queued_job(
        paths,
        RUN_ID,
        expected_revision=0,
        workload_class="test",
    )

    assert result["job"]["workload_class"] == "test"
    loaded_test, _state = load_job(paths, "rr-fedcba9876543210")
    test_lane = [
        job["run_id"]
        for job, _state in list_queued(paths)
        if job["workload_class"] == "test"
    ]
    assert test_lane == ["rr-fedcba9876543210", RUN_ID]


def test_historical_job_without_queue_priority_defaults_to_normal(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())
    job_path = paths.queue_dir / RUN_ID / "job.yaml"
    historical = load_yaml(job_path)
    historical.pop("queue_priority")
    write_yaml(job_path, historical)

    loaded, _state = load_job(paths, RUN_ID)

    assert loaded["queue_priority"] == "normal"


def test_historical_job_without_minimum_cores_defaults_to_one(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())
    job_path = paths.queue_dir / RUN_ID / "job.yaml"
    historical = load_yaml(job_path)
    historical.pop("minimum_cores")
    write_yaml(job_path, historical)

    loaded, _state = load_job(paths, RUN_ID)

    assert loaded["minimum_cores"] == 1


def test_schema_four_job_defaults_to_exclusive_legacy_identity(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())
    job_path = paths.queue_dir / RUN_ID / "job.yaml"
    historical = load_yaml(job_path)
    historical["schema_version"] = 4
    historical.pop("requested_cores")
    for server in historical["prepared_servers"]:
        for field in (
            "machine_id",
            "machine_id_source",
            "machine_fingerprint",
            "configured_memory_gb",
        ):
            server.pop(field)
    write_yaml(job_path, historical)

    loaded, _state = load_job(paths, RUN_ID)

    assert loaded["requested_cores"] is None
    assert loaded["prepared_servers"][0]["machine_id"] == "compute-a"
    assert loaded["prepared_servers"][0]["machine_fingerprint"] is None


def test_schema_two_job_preserves_explicit_all_scope_during_upgrade(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    dynamic = queued_job()
    dynamic["server_scope"] = "all"
    dynamic["output_path"] = None
    submit_job(paths, dynamic)
    job_path = paths.queue_dir / RUN_ID / "job.yaml"
    historical = load_yaml(job_path)
    historical["schema_version"] = 2
    write_yaml(job_path, historical)

    loaded, _state = load_job(paths, RUN_ID)

    assert loaded["server_scope"] == "all"


def test_historical_queue_schema_keeps_legacy_output_readable(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())
    job_path = paths.queue_dir / RUN_ID / "job.yaml"
    historical = load_yaml(job_path)
    historical["schema_version"] = 1
    historical.pop("output_relpath")
    historical["output_path"] = "$HOME/result.json"
    historical["prepared_servers"][0].pop("output_root")
    write_yaml(job_path, historical)

    loaded, _state = load_job(paths, RUN_ID)

    assert loaded["output_path"] == "$HOME/result.json"
    assert loaded["output_relpath"] is None
    assert loaded["prepared_servers"][0]["output_root"] is None
    assert "result_intent" not in loaded
    assert "result_tags" not in loaded


def test_relative_output_requires_every_prepared_root(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    invalid = queued_job()
    invalid["output_relpath"] = "validation/result.json"
    invalid["output_path"] = None

    with pytest.raises(ValueError, match="every prepared output_root"):
        submit_job(paths, invalid)


def test_job_rejects_prepared_server_below_minimum_cores(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    invalid = queued_job()
    invalid["minimum_cores"] = 512

    with pytest.raises(ValueError, match="fewer than the required 512 cores"):
        submit_job(paths, invalid)


def test_queue_state_is_forward_only(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())

    state = transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=0,
        status="dispatching",
    )
    assert state["status"] == "dispatching"
    with pytest.raises(ValueError, match="illegal queued state"):
        transition_queued_state(
            paths,
            RUN_ID,
            expected_revision=1,
            status="queued",
        )


def test_interrupted_dispatch_has_explicit_recovery_path(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())
    state = transition_queued_state(
        paths,
        RUN_ID,
        expected_revision=0,
        status="dispatching",
    )

    assert list_queued(paths) == []
    assert list_jobs(paths, statuses={"dispatching"})[0][1]["status"] == "dispatching"

    recovered = recover_dispatching_state(
        paths,
        RUN_ID,
        expected_revision=int(state["revision"]),
    )

    assert recovered["status"] == "queued"
    assert recovered["error"] == "recovered after interrupted dispatch"
    assert list_queued(paths)[0][0]["run_id"] == RUN_ID


def test_dispatch_lease_is_fenced_and_cannot_be_stolen_after_heartbeat_expiry(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, queued_job())

    original = acquire_dispatch_lease(
        paths,
        server="compute-a",
        run_id=RUN_ID,
        ttl_seconds=120,
        now=1000,
    )
    assert original is not None
    assert has_unexpired_dispatch_lease(paths, run_id=RUN_ID, now=1001)
    assert not acquire_dispatch_lease(
        paths,
        server="compute-a",
        run_id="rr-fedcba9876543210",
        ttl_seconds=120,
        now=1001,
    )
    assert not acquire_dispatch_lease(
        paths,
        server="compute-a",
        run_id="rr-fedcba9876543210",
        ttl_seconds=120,
        now=1121,
    )
    fenced = acquire_dispatch_lease(
        paths,
        server="compute-a",
        run_id=RUN_ID,
        ttl_seconds=120,
        now=1121,
    )
    assert fenced is not None
    assert fenced.token != original.token
    assert renew_dispatch_lease(
        paths,
        original,
        ttl_seconds=120,
        now=1122,
    ) is None
    assert release_dispatch_lease(
        paths,
        server="compute-a",
        run_id=RUN_ID,
        owner_token=fenced.token,
    )
    assert acquire_dispatch_lease(
        paths,
        server="compute-a",
        run_id="rr-fedcba9876543210",
        ttl_seconds=120,
        now=1122,
    )


def test_dispatch_lease_is_controller_global_across_projects(tmp_path: Path) -> None:
    root = tmp_path / "controller"
    first = controller_paths(root, "project-a")
    second = controller_paths(root, "project-b")

    ownership = acquire_dispatch_lease(
        first,
        server="compute-a",
        run_id=RUN_ID,
        ttl_seconds=120,
        now=1000,
    )
    assert ownership is not None
    assert not acquire_dispatch_lease(
        second,
        server="compute-a",
        run_id="rr-fedcba9876543210",
        ttl_seconds=120,
        now=1001,
    )
    assert not release_dispatch_lease(second, server="compute-a", run_id=RUN_ID)

    scheduler = controller_scheduler_paths(root)
    lease = load_yaml(scheduler.leases_dir / "compute-a.yaml")
    assert lease["project_id"] == "project-a"
    assert not release_dispatch_lease(first, server="compute-a", run_id=RUN_ID)
    assert release_dispatch_lease(
        first,
        server="compute-a",
        run_id=RUN_ID,
        owner_token=ownership.token,
    )
    assert acquire_dispatch_lease(
        second,
        server="compute-a",
        run_id="rr-fedcba9876543210",
        ttl_seconds=120,
        now=1001,
    )


def test_server_drain_is_controller_global_and_blocks_new_leases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "controller"
    first = controller_paths(root, "project-a")
    second = controller_paths(root, "project-b")
    ownership = acquire_dispatch_lease(
        first,
        server="burst",
        run_id="rr-fedcba9876543210",
        ttl_seconds=120,
    )
    assert ownership is not None

    drained = set_server_drained(first, "burst", drained=True)

    assert drained["changed"] is True
    assert drained["in_flight_dispatch"]["run_id"] == "rr-fedcba9876543210"
    assert list_drained_servers(second)["burst"]["requested_by_project"] == "project-a"
    assert not acquire_dispatch_lease(
        second,
        server="burst",
        run_id=RUN_ID,
        ttl_seconds=120,
    )
    assert (
        acquire_maintenance_lease(
            second,
            server="burst",
            run_id=RUN_ID,
            ttl_seconds=120,
        )
        is None
    )
    assert set_server_drained(second, "burst", drained=True)["changed"] is False
    assert release_dispatch_lease(
        first,
        server="burst",
        run_id="rr-fedcba9876543210",
        owner_token=ownership.token,
    )
    maintenance = acquire_maintenance_lease(
        second,
        server="burst",
        run_id=RUN_ID,
        ttl_seconds=120,
    )
    assert maintenance is not None
    scheduler = controller_scheduler_paths(root)
    assert load_yaml(scheduler.leases_dir / "burst.yaml")["kind"] == "maintenance"
    assert not acquire_dispatch_lease(
        first,
        server="burst",
        run_id="rr-1111111111111111",
        ttl_seconds=120,
    )
    assert release_dispatch_lease(
        second,
        server="burst",
        run_id=RUN_ID,
        owner_token=maintenance.token,
    )
    assert set_server_drained(second, "burst", drained=False)["changed"] is True
    assert list_drained_servers(first) == {}
    assert acquire_dispatch_lease(
        first,
        server="burst",
        run_id=RUN_ID,
        ttl_seconds=120,
    )


def test_malformed_lease_blocks_acquisition_without_overwrite(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    scheduler = controller_scheduler_paths(paths.root)
    scheduler.leases_dir.mkdir(parents=True)
    lease_path = scheduler.leases_dir / "compute-a.yaml"
    lease_path.write_text("project_id: [unterminated\n", encoding="utf-8")
    original = lease_path.read_bytes()

    with pytest.raises(MalformedLeaseError, match="malformed dispatch lease"):
        acquire_dispatch_lease(
            paths,
            server="compute-a",
            run_id=RUN_ID,
            ttl_seconds=120,
        )

    assert lease_path.read_bytes() == original


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 2,
            "server": "compute-a",
            "machine_id": "compute-a",
            "project_id": "example",
            "run_id": RUN_ID,
            "kind": "dispatch",
            "owner_token": "a" * 64,
            "created_at": 1000.0,
            "expires_at": 1120.0,
        },
        {
            "schema_version": 2,
            "server": "compute-a",
            "machine_id": "compute-a",
            "project_id": "example",
            "run_id": RUN_ID,
            "kind": "dispatch",
            "owner_token": "a" * 64,
            "created_at": 1000.0,
            "heartbeat_at": float("inf"),
            "expires_at": float("inf"),
        },
    ],
)
def test_malformed_lease_fields_fail_closed(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    scheduler = controller_scheduler_paths(paths.root)
    scheduler.leases_dir.mkdir(parents=True)
    lease_path = scheduler.leases_dir / "compute-a.yaml"
    write_yaml(lease_path, payload)

    with pytest.raises(MalformedLeaseError, match="malformed dispatch lease"):
        acquire_dispatch_lease(
            paths,
            server="compute-a",
            run_id="rr-fedcba9876543210",
            ttl_seconds=120,
        )


def test_non_utf8_lease_is_diagnosed_and_preserved(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    scheduler = controller_scheduler_paths(paths.root)
    scheduler.leases_dir.mkdir(parents=True)
    lease_path = scheduler.leases_dir / "compute-a.yaml"
    lease_path.write_bytes(b"\xff\xfe")

    with pytest.raises(MalformedLeaseError, match="malformed dispatch lease"):
        acquire_dispatch_lease(
            paths,
            server="compute-a",
            run_id=RUN_ID,
            ttl_seconds=120,
        )

    assert lease_path.read_bytes() == b"\xff\xfe"


def _identified_server(
    name: str,
    machine_id: str,
    fingerprint: str,
    *,
    cores: int = 256,
) -> dict[str, object]:
    return {
        "name": name,
        "machine_id": machine_id,
        "machine_id_source": "explicit",
        "machine_fingerprint": fingerprint,
        "configured_cores": cores,
        "configured_memory_gb": 512,
        "standard_slots": 1,
        "test_slots": 1,
    }


def test_machine_identity_shares_capacity_across_project_aliases(tmp_path: Path) -> None:
    root = tmp_path / "controller"
    first = controller_paths(root, "project-a")
    second = controller_paths(root, "project-b")
    fingerprint = "sha256:" + "a" * 64

    ensure_server_capacities(
        first,
        [_identified_server("compute-a", "physical-a", fingerprint)],
    )
    ensure_server_capacities(
        second,
        [_identified_server("alias-a", "physical-a", fingerprint)],
    )
    updated = update_server_capacity(
        first,
        "compute-a",
        machine_id="physical-a",
        expected_revision=0,
        standard_slots=3,
        test_slots=2,
    )

    assert updated["capacity"]["standard_slots"] == 3
    assert list_server_capacities(second)["physical-a"]["test_slots"] == 2


def test_legacy_machine_identity_migrates_without_losing_capacity_or_drain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "controller"
    paths = controller_paths(root, "project-a")
    fingerprint = "sha256:" + "f" * 64
    legacy = _identified_server("compute-a", "compute-a", fingerprint)
    legacy["machine_id_source"] = "legacy-name"

    ensure_server_capacities(paths, [legacy])
    update_server_capacity(
        paths,
        "compute-a",
        expected_revision=0,
        standard_slots=3,
        test_slots=2,
    )
    set_server_drained(paths, "compute-a", drained=True)

    explicit = _identified_server("compute-a", "physical-a", fingerprint)
    capacities = ensure_server_capacities(paths, [explicit])

    assert "compute-a" not in capacities
    assert capacities["physical-a"]["customized"] is True
    assert capacities["physical-a"]["standard_slots"] == 3
    drains = list_drained_servers(paths)
    assert set(drains) == {"physical-a"}
    assert drains["physical-a"]["requested_by_project"] == "project-a"
    machines = load_yaml(controller_scheduler_paths(root).machines_path)["machines"]
    assert machines["physical-a"]["legacy_machine_ids"] == ["compute-a"]
    assert machines["physical-a"]["aliases"] == ["project-a/compute-a"]

    legacy_alias = _identified_server("alias-a", "alias-a", fingerprint)
    legacy_alias["machine_id_source"] = "legacy-name"
    alias_capacities = ensure_server_capacities(
        controller_paths(root, "project-b"), [legacy_alias]
    )
    assert legacy_alias["machine_id"] == "physical-a"
    assert alias_capacities["physical-a"]["standard_slots"] == 3


def test_expired_legacy_dispatch_lease_is_fenced_during_identity_migration(
    tmp_path: Path,
) -> None:
    root = tmp_path / "controller"
    first = controller_paths(root, "project-a")
    second = controller_paths(root, "project-b")
    submit_job(first, queued_job())
    fingerprint = "sha256:" + "1" * 64
    legacy = _identified_server("compute-a", "compute-a", fingerprint)
    legacy["machine_id_source"] = "legacy-name"
    ensure_server_capacities(first, [legacy])
    scheduler = controller_scheduler_paths(root)
    scheduler.leases_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(
        scheduler.leases_dir / "compute-a.yaml",
        {
            "server": "compute-a",
            "project_id": "project-a",
            "run_id": RUN_ID,
            "kind": "dispatch",
            "created_at": 1000.0,
            "expires_at": 1001.0,
        },
    )

    ensure_server_capacities(
        first,
        [_identified_server("compute-a", "physical-a", fingerprint)],
    )
    ensure_server_capacities(
        second,
        [_identified_server("alias-a", "physical-a", fingerprint)],
    )

    assert acquire_dispatch_lease(
        second,
        server="alias-a",
        machine_id="physical-a",
        run_id="rr-fedcba9876543210",
        ttl_seconds=120,
        now=2000,
    ) is None
    fenced = acquire_dispatch_lease(
        first,
        server="compute-a",
        machine_id="physical-a",
        run_id=RUN_ID,
        ttl_seconds=120,
        now=2000,
    )
    assert fenced is not None
    assert not (scheduler.leases_dir / "compute-a.yaml").exists()
    assert release_dispatch_lease(
        first,
        server="compute-a",
        machine_id="physical-a",
        run_id=RUN_ID,
        owner_token=fenced.token,
    )
    assert acquire_dispatch_lease(
        second,
        server="alias-a",
        machine_id="physical-a",
        run_id="rr-fedcba9876543210",
        ttl_seconds=120,
        now=2001,
    ) is not None


def test_machine_identity_conflicts_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "controller"
    first = controller_paths(root, "project-a")
    second = controller_paths(root, "project-b")
    fingerprint = "sha256:" + "b" * 64
    ensure_server_capacities(
        first,
        [_identified_server("compute-a", "physical-a", fingerprint)],
    )

    with pytest.raises(ValueError, match="explicit and cannot be reassigned"):
        ensure_server_capacities(
            second,
            [_identified_server("alias-a", "physical-b", fingerprint)],
        )
    with pytest.raises(ValueError, match="different physical fingerprint"):
        ensure_server_capacities(
            second,
            [
                _identified_server(
                    "alias-a",
                    "physical-a",
                    "sha256:" + "c" * 64,
                )
            ],
        )


def test_same_display_name_can_refer_to_distinct_explicit_machines(
    tmp_path: Path,
) -> None:
    root = tmp_path / "controller"
    ensure_server_capacities(
        controller_paths(root, "project-a"),
        [
            _identified_server(
                "compute",
                "physical-a",
                "sha256:" + "d" * 64,
            )
        ],
    )
    capacities = ensure_server_capacities(
        controller_paths(root, "project-b"),
        [
            _identified_server(
                "compute",
                "physical-b",
                "sha256:" + "e" * 64,
            )
        ],
    )

    assert {"physical-a", "physical-b"} <= set(capacities)
