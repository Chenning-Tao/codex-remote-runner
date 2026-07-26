from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from remote_runner._internal import queue_control
from remote_runner._internal.execution_registry import write_yaml


RUN_ID = "rr-0123456789abcdef"


def project_config(tmp_path: Path) -> Path:
    path = tmp_path / ".remote-runner.yaml"
    write_yaml(
        path,
        {
            "project_id": "example",
            "controller": {"ssh": "controller", "root": "/controller"},
            "source": {"local_repo": "code"},
            "remote": {
                name: {
                    "bare_repo": f"/srv/{name}/repo.git",
                    "worktree_root": f"/srv/{name}/worktrees",
                    "python": f"/opt/{name}/python3",
                }
                for name in ("compute-a", "compute-b")
            },
            "scheduling": {"testing": {"servers": ["compute-b"]}},
        },
    )
    (tmp_path / "code").mkdir()
    return path


def arguments(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_config=project_config(tmp_path),
        source_repo=None,
        server_registry=tmp_path / "servers.yaml",
        ssh_profile="auto",
        timeout=8,
        prepare_timeout=60,
    )


def test_queue_update_prepares_missing_servers_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def controller(_config, action, *, payload=None, **_kwargs):
        calls.append((action, payload))
        if action == "queued-job":
            return {"job": {"run_id": RUN_ID, "prepared_servers": ["compute-a"]}}
        if action == "reserve-queue-update":
            return {"token": "reservation-token", "state": {"revision": 4}}
        if action == "update-queued-job":
            return {"changed": True}
        raise AssertionError(f"unexpected controller action: {action}")

    additions: list[argparse.Namespace] = []
    monkeypatch.setattr(queue_control, "call_controller", controller)
    monkeypatch.setattr(
        queue_control.server_addition,
        "add",
        lambda args: additions.append(args) or {"outcome": {"action": "extended"}},
    )
    payload = {
        "expected_revision": 3,
        "queue_priority": "normal",
        "eligible_servers": ["compute-a", "compute-b"],
    }

    result = queue_control.request_queue_update(
        arguments(tmp_path),
        RUN_ID,
        payload,
    )

    assert result == {"changed": True}
    assert [action for action, _payload in calls] == [
        "queued-job",
        "reserve-queue-update",
        "update-queued-job",
    ]
    assert additions[0].server == "compute-b"
    assert additions[0].placement_token == "reservation-token"
    assert calls[1][1] == {
        "expected_revision": 3,
        "requested_servers": ["compute-a", "compute-b"],
        "ttl_seconds": 120,
    }
    assert calls[2][1] == {
        **payload,
        "expected_revision": 4,
        "placement_token": "reservation-token",
    }


def test_queue_workload_switch_prepares_server_for_target_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def controller(_config, action, *, payload=None, **_kwargs):
        if action == "queued-job":
            return {"job": {"run_id": RUN_ID, "prepared_servers": ["compute-a"]}}
        if action == "reserve-queue-update":
            return {"token": "reservation-token", "state": {"revision": 2}}
        if action == "update-queued-job":
            return {"changed": True}
        raise AssertionError(f"unexpected controller action: {action}")

    additions: list[argparse.Namespace] = []
    monkeypatch.setattr(queue_control, "call_controller", controller)
    monkeypatch.setattr(
        queue_control.server_addition,
        "add",
        lambda args: additions.append(args) or {"outcome": {"action": "extended"}},
    )

    queue_control.request_queue_update(
        arguments(tmp_path),
        RUN_ID,
        {
            "expected_revision": 1,
            "workload_class": "test",
            "eligible_servers": ["compute-b"],
        },
    )

    assert additions[0].server == "compute-b"
    assert additions[0].target_workload_class == "test"


def test_queue_update_releases_reservation_after_preparation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def controller(_config, action, *, payload=None, **_kwargs):
        calls.append((action, payload))
        if action == "queued-job":
            return {"job": {"run_id": RUN_ID, "prepared_servers": ["compute-a"]}}
        if action == "reserve-queue-update":
            return {"token": "reservation-token", "state": {"revision": 2}}
        if action == "release-queue-update":
            return {"changed": True}
        raise AssertionError(f"unexpected controller action: {action}")

    monkeypatch.setattr(queue_control, "call_controller", controller)
    monkeypatch.setattr(
        queue_control.server_addition,
        "add",
        lambda _args: (_ for _ in ()).throw(RuntimeError("push failed")),
    )

    with pytest.raises(queue_control.QueuePreparationError, match="push failed"):
        queue_control.request_queue_update(
            arguments(tmp_path),
            RUN_ID,
            {
                "expected_revision": 1,
                "eligible_servers": ["compute-b"],
            },
        )

    assert [action for action, _payload in calls] == [
        "queued-job",
        "reserve-queue-update",
        "release-queue-update",
    ]
    assert calls[-1][1] == {"token": "reservation-token"}
