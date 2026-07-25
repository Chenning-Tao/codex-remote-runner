from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from remote_runner._internal import monitoring
from remote_runner._internal.output_sync import load_config
from remote_runner._internal.controller import service as controller_service
from remote_runner._internal.controller.registry import (
    controller_paths,
    load_job,
    submit_job,
)
from remote_runner._internal.execution_registry import sha256_bytes, write_yaml


def test_ensure_dispatcher_checks_an_exact_project_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append([str(item) for item in argv])
        if argv[1] == "has-session":
            return subprocess.CompletedProcess(argv, 1)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(controller_service, "resolve_tmux_executable", lambda: "tmux")
    monkeypatch.setattr(controller_service.subprocess, "run", fake_run)

    started = controller_service.ensure_dispatcher(
        controller_root=tmp_path / "controller",
        project_id="example_project",
        timeout=8,
        interval=60,
    )

    assert started is True
    assert calls[0] == [
        "tmux",
        "has-session",
        "-t",
        "=rr-dispatch-example_project",
    ]
    assert calls[1][0:6] == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "rr-dispatch-example_project",
        sys.executable,
    ]


def job() -> dict[str, object]:
    command = "python experiment.py --num-workers 2"
    return {
        "run_id": "rr-0123456789abcdef",
        "revision": "a" * 40,
        "label": "test",
        "task_id": "task-1",
        "result_intent": "candidate",
        "result_tags": {"campaign": "test"},
        "submitted_command": command,
        "submitted_command_sha256": sha256_bytes(command.encode()),
        "worker_arg": "--num-workers",
        "prepared_servers": [
            {
                "name": "compute-a",
                "ssh": "compute-a",
                "ssh_profile": "intranet",
                "configured_cores": 256,
                "priority": 100,
                "bare_repo": "/srv/repo.git",
                "worktree_root": "/srv/worktrees",
                "python": "/opt/python3",
                "output_root": None,
            }
        ],
        "output_relpath": None,
        "output_path": None,
        "output_metadata": {},
    }


def test_submit_persists_job_and_starts_dispatcher_when_queued(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(job())))
    started: list[str] = []
    monkeypatch.setattr(
        controller_service,
        "ensure_dispatcher",
        lambda **kwargs: started.append(str(kwargs["project_id"])) or True,
    )
    args = argparse.Namespace(
        controller_root=tmp_path / "controller",
        project_id="example",
        timeout=8,
        interval=30,
    )

    result = controller_service.submit(args)

    assert result["outcome"] == {
        "action": "submitted",
        "run_id": "rr-0123456789abcdef",
    }
    assert result["dispatcher_started"] is True
    assert started == ["example"]


def test_edit_queued_job_updates_priority_and_starts_dispatcher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"expected_revision": 0, "queue_priority": "urgent"})),
    )
    started: list[str] = []
    monkeypatch.setattr(
        controller_service,
        "ensure_dispatcher",
        lambda **kwargs: started.append(str(kwargs["project_id"])) or True,
    )

    result = controller_service.edit_queued_job(
        argparse.Namespace(
            controller_root=paths.root,
            project_id="example",
            run_id="rr-0123456789abcdef",
            timeout=8,
            interval=30,
        )
    )

    assert result["changed"] is True
    assert result["job"]["queue_priority"] == "urgent"
    assert result["state"]["revision"] == 1
    assert result["dispatcher_started"] is True
    assert started == ["example"]


def test_queue_update_reservation_exposes_no_token_digest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "expected_revision": 0,
                    "requested_servers": ["compute-a", "compute-b"],
                    "ttl_seconds": 120,
                }
            )
        ),
    )
    args = argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        run_id="rr-0123456789abcdef",
        timeout=8,
        interval=30,
    )

    result = controller_service.reserve_queue_update(args)

    assert isinstance(result["token"], str)
    assert result["state"]["revision"] == 1
    assert result["state"]["placement_update"]["status"] == "preparing"
    assert "token_sha256" not in result["state"]["placement_update"]


def test_submit_persists_output_sync_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = job()
    payload["output_sync"] = {
        "schema_version": 1,
        "target_server": "archive",
        "target_ssh": "archive",
        "target_root": "/srv/archive",
        "target_python": "/opt/python3",
        "source_ssh_config": "/home/user/.ssh/output-sync.conf",
        "source_hosts": {"compute-a": "compute-a-int"},
        "retry_seconds": 60,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        controller_service, "ensure_dispatcher", lambda **_kwargs: False
    )
    args = argparse.Namespace(
        controller_root=tmp_path / "controller",
        project_id="example",
        timeout=8,
        interval=30,
    )

    controller_service.submit(args)

    paths = controller_paths(tmp_path / "controller", "example")
    config = load_config(paths.registry_root)
    assert config is not None
    assert config.target_server == "archive"
    assert config.source_hosts == {"compute-a": "compute-a-int"}


def test_queued_job_returns_only_extension_constraints(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())

    result = controller_service.queued_job(
        argparse.Namespace(
            controller_root=paths.root,
            project_id="example",
            run_id="rr-0123456789abcdef",
        )
    )

    assert result == {
        "job": {
            "run_id": "rr-0123456789abcdef",
            "revision": "a" * 40,
            "minimum_cores": 1,
            "workload_class": "standard",
            "prepared_servers": ["compute-a"],
            "output_relpath": None,
            "output_path": None,
        }
    }


def test_extend_job_appends_server_and_wakes_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    addition = {
        **job()["prepared_servers"][0],
        "name": "archive",
        "ssh": "archive",
        "configured_cores": 128,
    }
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "revision": "a" * 40,
                    "prepared_servers": [addition],
                }
            )
        ),
    )
    started: list[str] = []
    monkeypatch.setattr(
        controller_service,
        "ensure_dispatcher",
        lambda **kwargs: started.append(str(kwargs["project_id"])) or True,
    )

    result = controller_service.extend_job(
        argparse.Namespace(
            controller_root=paths.root,
            project_id="example",
            run_id="rr-0123456789abcdef",
            timeout=8,
            interval=60,
        )
    )

    assert result == {
        "run_id": "rr-0123456789abcdef",
        "status": "extended",
        "added_servers": 1,
        "prepared_servers": ["compute-a", "archive"],
        "dispatcher_started": True,
    }
    assert started == ["example"]
    persisted, _state = load_job(paths, "rr-0123456789abcdef")
    assert [server["name"] for server in persisted["prepared_servers"]] == [
        "compute-a",
        "archive",
    ]


def test_drain_server_counts_frozen_queue_matches_and_resume_wakes_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    started: list[str] = []
    monkeypatch.setattr(
        controller_service,
        "ensure_dispatcher",
        lambda **kwargs: started.append(str(kwargs["project_id"])) or True,
    )
    args = argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        server="compute-a",
        timeout=8,
        interval=60,
    )

    drained = controller_service.update_server_drain(args, drained=True)
    resumed = controller_service.update_server_drain(args, drained=False)

    assert drained["scope"] == "controller"
    assert drained["project_queued_matches"] == 1
    assert drained["dispatcher_started"] is False
    assert resumed["changed"] is True
    assert resumed["dispatcher_started"] is True
    assert started == ["example"]


def test_status_returns_terminal_queue_record_for_explicit_run(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    controller_service.transition_queued_state(
        paths,
        "rr-0123456789abcdef",
        expected_revision=0,
        status="stopped",
    )
    args = argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        run_id="rr-0123456789abcdef",
        timeout=8,
        interval=60,
    )

    result = controller_service.status(args)

    assert result["queue"][0]["state"]["status"] == "stopped"
    assert result["runs"] == []
    assert result["output_sync"] == {"status": "not_enqueued"}
    assert result["run_view"]["phase"] == "terminal"
    assert result["run_view"]["outcome"] == "stopped"
    assert result["run_view"]["terminal_source"] == "queue"


def test_wait_run_returns_heartbeat_for_unchanged_active_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    initial = controller_service.load_run_view(paths, "rr-0123456789abcdef")
    started: list[str] = []
    monkeypatch.setattr(
        controller_service,
        "ensure_dispatcher",
        lambda **kwargs: started.append(str(kwargs["project_id"])) or True,
    )

    result = controller_service.wait_run(
        argparse.Namespace(
            controller_root=paths.root,
            project_id="example",
            run_id="rr-0123456789abcdef",
            after_etag=initial["etag"],
            wait_seconds=0,
            timeout=8,
            interval=60,
        )
    )

    assert result["changed"] is False
    assert result["timed_out"] is True
    assert result["dispatcher_started"] is True
    assert result["run_view"]["phase"] == "queued"
    assert started == ["example"]


def test_wait_run_returns_changed_terminal_state_without_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    initial = controller_service.load_run_view(paths, "rr-0123456789abcdef")
    controller_service.transition_queued_state(
        paths,
        "rr-0123456789abcdef",
        expected_revision=0,
        status="stopped",
    )

    def unexpected_dispatcher(**_kwargs):
        raise AssertionError("terminal state must not start the dispatcher")

    monkeypatch.setattr(
        controller_service,
        "ensure_dispatcher",
        unexpected_dispatcher,
    )
    result = controller_service.wait_run(
        argparse.Namespace(
            controller_root=paths.root,
            project_id="example",
            run_id="rr-0123456789abcdef",
            after_etag=initial["etag"],
            wait_seconds=55,
            timeout=8,
            interval=60,
        )
    )

    assert result["changed"] is True
    assert result["timed_out"] is False
    assert result["run_view"]["phase"] == "terminal"
    assert result["run_view"]["outcome"] == "stopped"


def test_wait_run_returns_stable_attention_state_without_timing_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    controller_service.transition_queued_state(
        paths,
        "rr-0123456789abcdef",
        expected_revision=0,
        status="dispatching",
    )
    controller_service.transition_queued_state(
        paths,
        "rr-0123456789abcdef",
        expected_revision=1,
        status="dispatched",
    )
    initial = controller_service.load_run_view(paths, "rr-0123456789abcdef")
    monkeypatch.setattr(
        controller_service, "ensure_dispatcher", lambda **_kwargs: False
    )

    result = controller_service.wait_run(
        argparse.Namespace(
            controller_root=paths.root,
            project_id="example",
            run_id="rr-0123456789abcdef",
            after_etag=initial["etag"],
            wait_seconds=55,
            timeout=8,
            interval=60,
        )
    )

    assert result["changed"] is False
    assert result["timed_out"] is False
    assert result["run_view"]["phase"] == "attention_required"


def _wait_runs(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    runs: list[dict[str, object]],
    *,
    wait_seconds: int = 0,
) -> dict[str, object]:
    monkeypatch.setattr(
        controller_service.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "schema_version": 1,
                    "wait_seconds": wait_seconds,
                    "runs": runs,
                }
            )
        ),
    )
    return controller_service.wait_runs(
        argparse.Namespace(
            controller_root=paths.root,
            project_id="example",
            timeout=8,
            interval=60,
        )
    )


def test_wait_runs_returns_one_ordered_snapshot_for_a_changed_cohort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    first = job()
    second = job()
    second["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, first)
    submit_job(paths, second)
    monkeypatch.setattr(
        controller_service, "ensure_dispatcher", lambda **_kwargs: False
    )

    result = _wait_runs(
        monkeypatch,
        paths,
        [
            {"run_id": first["run_id"], "after_etag": None},
            {"run_id": second["run_id"], "after_etag": None},
        ],
    )

    assert result["changed"] is True
    assert result["ready"] is False
    assert result["timed_out"] is False
    assert result["changed_run_ids"] == [first["run_id"], second["run_id"]]
    assert [view["run_id"] for view in result["run_views"]] == [
        first["run_id"],
        second["run_id"],
    ]


def test_wait_runs_does_not_spin_on_one_stable_terminal_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    first = job()
    second = job()
    second["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, first)
    submit_job(paths, second)
    controller_service.transition_queued_state(
        paths,
        first["run_id"],
        expected_revision=0,
        status="stopped",
    )
    initial = {
        run_id: controller_service.load_run_view(paths, run_id)["etag"]
        for run_id in (first["run_id"], second["run_id"])
    }
    monkeypatch.setattr(
        controller_service, "ensure_dispatcher", lambda **_kwargs: False
    )

    result = _wait_runs(
        monkeypatch,
        paths,
        [
            {"run_id": first["run_id"], "after_etag": initial[first["run_id"]]},
            {"run_id": second["run_id"], "after_etag": initial[second["run_id"]]},
        ],
    )

    assert result["changed"] is False
    assert result["ready"] is False
    assert result["timed_out"] is True


def test_wait_runs_wakes_once_every_member_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    first = job()
    second = job()
    second["run_id"] = "rr-fedcba9876543210"
    submit_job(paths, first)
    submit_job(paths, second)
    for item in (first, second):
        controller_service.transition_queued_state(
            paths,
            item["run_id"],
            expected_revision=0,
            status="stopped",
        )
    etags = {
        item["run_id"]: controller_service.load_run_view(paths, item["run_id"])["etag"]
        for item in (first, second)
    }

    result = _wait_runs(
        monkeypatch,
        paths,
        [
            {"run_id": item["run_id"], "after_etag": etags[item["run_id"]]}
            for item in (first, second)
        ],
    )

    assert result["changed"] is False
    assert result["ready"] is True
    assert result["timed_out"] is False


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [
            {"run_id": "rr-0123456789abcdef", "after_etag": None},
            {"run_id": "rr-0123456789abcdef", "after_etag": None},
        ],
        [{"run_id": "rr-0123456789abcdef", "after_etag": "bad"}],
        [{"run_id": True, "after_etag": None}],
    ],
)
def test_wait_runs_rejects_invalid_cohort_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runs: list[dict[str, object]],
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")

    with pytest.raises(ValueError):
        _wait_runs(monkeypatch, paths, runs)


def test_status_filters_queue_and_runs_by_normalized_task_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    matching_job = job()
    matching_job["task_id"] = "tasks/07-18-example"
    submit_job(paths, matching_job)
    other_job = job()
    other_job["run_id"] = "rr-fedcba9876543210"
    other_job["task_id"] = "07-18-other"
    submit_job(paths, other_job)
    write_yaml(paths.config_path, {"controller_registry": True})
    execution_paths = object()
    rows = [
        {
            "run_id": "rr-0123456789abcdef",
            "task_id": "07-18-example",
            "registry_kind": "current",
            "authoritative_status": "running",
        },
        {
            "run_id": "rr-fedcba9876543210",
            "task_id": "archive/2026-07/07-18-other",
            "registry_kind": "current",
            "authoritative_status": "succeeded",
        },
    ]
    monitored: list[str] = []
    monkeypatch.setattr(
        controller_service, "project_paths", lambda _path: execution_paths
    )
    monkeypatch.setattr(
        monitoring, "load_registry_rows", lambda _paths, **_kwargs: rows
    )
    monkeypatch.setattr(
        monitoring,
        "monitor_row",
        lambda _paths, row, _timeout, *, no_write: (
            monitored.append(str(row["run_id"])) or row
        ),
    )
    monkeypatch.setattr(
        controller_service, "ensure_dispatcher", lambda **_kwargs: False
    )
    args = argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        run_id=None,
        task_id="archive/2026-07/07-18-example",
        timeout=8,
        interval=60,
    )

    result = controller_service.status(args)

    assert [item["job"]["run_id"] for item in result["queue"]] == [
        "rr-0123456789abcdef"
    ]
    assert [item["run_id"] for item in result["runs"]] == ["rr-0123456789abcdef"]
    assert monitored == ["rr-0123456789abcdef"]
    assert result["summary"] == {
        "queue": {
            "total": 1,
            "active": 1,
            "matched": 1,
            "returned": 1,
            "omitted": 0,
            "by_status": {"queued": 1},
            "by_result_intent": {"candidate": 1},
        },
        "runs": {
            "total": 1,
            "active": 1,
            "matched": 1,
            "returned": 1,
            "omitted": 0,
            "by_authoritative_status": {"running": 1},
            "by_result_intent": {"unclassified": 1},
        },
    }

    args.task_id = "07-18-missing"
    assert controller_service.status(args) == {
        "queue": [],
        "runs": [],
        "summary": {
            "queue": {
                "total": 0,
                "active": 0,
                "matched": 0,
                "returned": 0,
                "omitted": 0,
                "by_status": {},
                "by_result_intent": {},
            },
            "runs": {
                "total": 0,
                "active": 0,
                "matched": 0,
                "returned": 0,
                "omitted": 0,
                "by_authoritative_status": {},
                "by_result_intent": {},
            },
        },
    }


def test_status_lists_urgent_queued_work_before_normal_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    normal = job()
    submit_job(paths, normal, now="2026-01-01T00:00:00+00:00")
    urgent = job()
    urgent["run_id"] = "rr-fedcba9876543210"
    urgent["queue_priority"] = "urgent"
    submit_job(paths, urgent, now="2026-01-01T00:00:01+00:00")
    monkeypatch.setattr(
        controller_service, "ensure_dispatcher", lambda **_kwargs: False
    )
    args = argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        run_id=None,
        task_id=None,
        timeout=8,
        interval=60,
    )

    result = controller_service.status(args)

    assert [item["job"]["run_id"] for item in result["queue"]] == [
        "rr-fedcba9876543210",
        "rr-0123456789abcdef",
    ]


def test_status_overview_loads_the_queue_registry_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    original_list_jobs = controller_service.list_jobs
    calls = 0

    def counted_list_jobs(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_list_jobs(*args, **kwargs)

    monkeypatch.setattr(controller_service, "list_jobs", counted_list_jobs)
    monkeypatch.setattr(
        controller_service, "ensure_dispatcher", lambda **_kwargs: False
    )

    result = controller_service.status(
        argparse.Namespace(
            controller_root=paths.root,
            project_id="example",
            run_id=None,
            task_id=None,
            timeout=8,
            interval=60,
        )
    )

    assert calls == 1
    assert [item["job"]["run_id"] for item in result["queue"]] == [
        "rr-0123456789abcdef"
    ]


def test_status_filters_queue_by_result_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    supporting = job()
    supporting["run_id"] = "rr-fedcba9876543210"
    supporting["result_intent"] = "supporting"
    supporting["result_tags"] = {"purpose": "validation"}
    submit_job(paths, supporting)
    monkeypatch.setattr(
        controller_service, "ensure_dispatcher", lambda **_kwargs: False
    )
    args = argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        run_id=None,
        task_id=None,
        result_intent="supporting",
        timeout=8,
        interval=60,
    )

    result = controller_service.status(args)

    assert [item["job"]["run_id"] for item in result["queue"]] == [
        "rr-fedcba9876543210"
    ]
    assert result["summary"]["queue"]["by_result_intent"] == {"supporting": 1}


def test_status_defaults_to_active_records_with_complete_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    urgent_job = job()
    urgent_job["run_id"] = "rr-fedcba9876543210"
    urgent_job["queue_priority"] = "urgent"
    submit_job(paths, urgent_job)
    stopped_job = job()
    stopped_job["run_id"] = "rr-3333333333333333"
    submit_job(paths, stopped_job)
    controller_service.transition_queued_state(
        paths,
        "rr-3333333333333333",
        expected_revision=0,
        status="stopped",
    )
    write_yaml(paths.config_path, {"controller_registry": True})
    execution_paths = object()
    rows = [
        {
            "run_id": "rr-1111111111111111",
            "registry_kind": "current",
            "authoritative_status": "running",
            "remote_log": "/srv/runtime/log",
        },
        {
            "run_id": "rr-2222222222222222",
            "registry_kind": "current",
            "authoritative_status": "running",
        },
        {
            "run_id": "rr-3333333333333333",
            "registry_kind": "current",
            "authoritative_status": "succeeded",
        },
    ]
    monitored: list[str] = []
    monkeypatch.setattr(
        controller_service, "project_paths", lambda _path: execution_paths
    )
    monkeypatch.setattr(
        monitoring, "load_registry_rows", lambda _paths, **_kwargs: rows
    )
    monkeypatch.setattr(
        monitoring,
        "monitor_row",
        lambda _paths, row, _timeout, *, no_write: (
            monitored.append(str(row["run_id"])) or row
        ),
    )
    monkeypatch.setattr(
        controller_service, "ensure_dispatcher", lambda **_kwargs: False
    )
    monkeypatch.setattr(controller_service, "OVERVIEW_RECORD_LIMIT", 1)
    args = argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        run_id=None,
        task_id=None,
        timeout=8,
        interval=60,
    )

    result = controller_service.status(args)

    assert [item["job"]["run_id"] for item in result["queue"]] == [
        "rr-fedcba9876543210"
    ]
    assert "prepared_servers" not in result["queue"][0]["job"]
    assert result["queue"][0]["job"]["eligible_servers"] == ["compute-a"]
    assert [item["run_id"] for item in result["runs"]] == ["rr-1111111111111111"]
    assert "remote_log" not in result["runs"][0]
    assert monitored == ["rr-1111111111111111", "rr-2222222222222222"]
    assert result["summary"] == {
        "queue": {
            "total": 3,
            "active": 2,
            "matched": 2,
            "returned": 1,
            "omitted": 1,
            "by_status": {"queued": 2, "stopped": 1},
            "by_result_intent": {"candidate": 3},
        },
        "runs": {
            "total": 3,
            "active": 2,
            "matched": 2,
            "returned": 1,
            "omitted": 1,
            "by_authoritative_status": {"running": 2, "succeeded": 1},
            "by_result_intent": {"unclassified": 3},
        },
    }

    args._full_overview = True
    full_result = controller_service.status(args)

    assert len(full_result["queue"]) == 2
    assert len(full_result["runs"]) == 2
    assert full_result["summary"]["queue"]["returned"] == 2
    assert full_result["summary"]["queue"]["omitted"] == 0
    assert full_result["summary"]["runs"]["returned"] == 2
    assert full_result["summary"]["runs"]["omitted"] == 0


def test_status_is_not_blocked_by_output_sync_worker_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    write_yaml(paths.config_path, {"controller_registry": True})
    monkeypatch.setattr(
        controller_service,
        "ensure_output_sync_worker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("tmux failed")),
    )
    args = argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        run_id=None,
        task_id=None,
        timeout=8,
        interval=60,
    )

    result = controller_service.status(args)

    assert result["output_sync"]["worker_started"] is False
    assert result["output_sync"]["worker_error"] == "tmux failed"


def test_controller_status_rejects_run_and_task_selectors_together() -> None:
    parser = controller_service.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--controller-root",
                "/tmp/controller",
                "--project-id",
                "example",
                "status",
                "--run-id",
                "rr-0123456789abcdef",
                "--task-id",
                "07-18-example",
            ]
        )

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--controller-root",
                "/tmp/controller",
                "--project-id",
                "example",
                "status",
                "--all",
            ]
        )


def test_stop_cancels_queued_run(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    args = argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        run_id="rr-0123456789abcdef",
        timeout=8,
    )

    result = controller_service.stop(args)

    assert result["kind"] == "queue"
    assert result["state"]["status"] == "stopped"
    assert load_job(paths, "rr-0123456789abcdef")[1]["status"] == "stopped"


def test_stop_delegates_running_execution_to_existing_stop_logic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    submit_job(paths, job())
    state = load_job(paths, "rr-0123456789abcdef")[1]
    state = controller_service.transition_queued_state(
        paths,
        "rr-0123456789abcdef",
        expected_revision=int(state["revision"]),
        status="dispatching",
    )
    controller_service.transition_queued_state(
        paths,
        "rr-0123456789abcdef",
        expected_revision=int(state["revision"]),
        status="dispatched",
    )
    write_yaml(paths.config_path, {"controller_registry": True})
    execution_paths = object()
    monkeypatch.setattr(
        controller_service, "project_paths", lambda _path: execution_paths
    )
    monkeypatch.setattr(
        controller_service, "registry_kind", lambda _paths, _run_id: "current"
    )
    monkeypatch.setattr(
        controller_service,
        "stop_execution",
        lambda _paths, _run_id, _timeout: {"status": "stopped", "revision": 4},
    )
    args = argparse.Namespace(
        controller_root=paths.root,
        project_id="example",
        run_id="rr-0123456789abcdef",
        timeout=8,
    )

    result = controller_service.stop(args)

    assert result["kind"] == "run"
    assert result["state"]["status"] == "stopped"
    assert result["queue_state"]["status"] == "dispatched"
