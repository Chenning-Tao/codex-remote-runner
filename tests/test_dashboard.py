from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
from threading import Barrier

from remote_runner._internal.config import load_managed_project_config
from remote_runner._internal.controller import dashboard as controller_dashboard
from remote_runner._internal.controller import service as controller_service
from remote_runner._internal.dashboard import build_server_inventory
from remote_runner._internal.execution_registry import write_yaml


def project_config(tmp_path: Path) -> Path:
    path = tmp_path / ".remote-runner.yaml"
    write_yaml(
        path,
        {
            "project_id": "example",
            "controller": {"ssh": "controller_host", "root": "/srv/controller"},
            "source": {"local_repo": "code"},
            "remote": {
                "compute-b": {
                    "enabled": False,
                    "bare_repo": "/srv/repo.git",
                    "worktree_root": "/srv/worktrees",
                    "python": "/opt/python3",
                },
                "compute-a": {
                    "bare_repo": "/srv/repo.git",
                    "worktree_root": "/srv/worktrees",
                    "python": "/opt/python3",
                },
                "missing": {
                    "bare_repo": "/srv/repo.git",
                    "worktree_root": "/srv/worktrees",
                    "python": "/opt/python3",
                },
            },
        },
    )
    return path


def test_build_server_inventory_includes_idle_disabled_and_misconfigured_servers(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "servers.yaml"
    write_yaml(
        registry,
        {
            "servers": {
                "compute-b": {"ssh": "compute-b", "cores": 128},
                "compute-a": {
                    "cores": 256,
                    "endpoints": {"tailscale": "compute-a-ts"},
                    "endpoint_order": ["tailscale"],
                    "testing": {"slots": 2},
                },
            }
        },
    )

    inventory = build_server_inventory(
        load_managed_project_config(project_config(tmp_path)),
        registry,
    )

    by_name = {item["name"]: item for item in inventory}
    assert list(by_name) == ["compute-a", "compute-b", "missing"]
    assert by_name["compute-b"]["enabled"] is False
    assert by_name["compute-a"] == {
        "name": "compute-a",
        "enabled": True,
        "auto_select": True,
        "python": "/opt/python3",
        "configured_cores": 256,
        "test_slots": 2,
        "endpoints": [
            {"ssh": "compute-a-ts", "ssh_profile": "tailscale"},
            {"ssh": "compute-a", "ssh_profile": "ssh"},
        ],
    }
    assert by_name["missing"]["enabled"] is False
    assert "not in the global registry" in by_name["missing"]["configuration_error"]


def dashboard_server(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "compute-a",
        "enabled": True,
        "auto_select": True,
        "python": "/opt/python3",
        "configured_cores": 256,
        "test_slots": 2,
        "endpoints": [{"ssh": "compute-a", "ssh_profile": "tailscale"}],
        "configuration_error": None,
    }
    value.update(changes)
    return value


def test_server_snapshot_preserves_capacity_and_enriches_known_runs(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        controller_dashboard,
        "probe_server_state",
        lambda *_args: {
            "reachable": True,
            "load1": 10.0,
            "load5": 8.0,
            "load15": 6.0,
            "remote_cores": 256,
            "active_runs": (
                {"run_id": "rr-0123456789abcdef", "workload_class": "standard"},
            ),
            "active_run_ids": ("rr-0123456789abcdef",),
        },
    )
    validated = controller_dashboard.validate_payload(
        {"schema_version": 1, "servers": [dashboard_server()]}
    )

    snapshot = controller_dashboard.collect_server_snapshot(validated, timeout=1)
    enriched = controller_dashboard.enrich_active_runs(
        snapshot,
        [
            {
                "run_id": "rr-0123456789abcdef",
                "label": "decoder sweep",
                "progress": {"percent": 42.0},
            }
        ],
    )

    assert enriched[0]["state"] == "busy"
    assert enriched[0]["configured_cores"] == 256
    assert enriched[0]["standard_runs"] == 1
    assert enriched[0]["test_runs"] == 0
    assert enriched[0]["active_runs"][0]["label"] == "decoder sweep"
    assert enriched[0]["active_runs"][0]["progress"] == {"percent": 42.0}


def test_server_snapshot_keeps_remote_label_for_unregistered_active_run(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        controller_dashboard,
        "probe_server_state",
        lambda *_args: {
            "reachable": True,
            "load1": 10.0,
            "load5": 8.0,
            "load15": 6.0,
            "remote_cores": 256,
            "active_runs": (
                {
                    "run_id": "rr-0123456789abcdef",
                    "label": "hardware test",
                    "workload_class": "test",
                },
            ),
            "active_run_ids": ("rr-0123456789abcdef",),
        },
    )
    validated = controller_dashboard.validate_payload(
        {"schema_version": 1, "servers": [dashboard_server()]}
    )

    snapshot = controller_dashboard.collect_server_snapshot(validated, timeout=1)
    enriched = controller_dashboard.enrich_active_runs(snapshot, [])

    assert enriched[0]["active_runs"][0]["label"] == "hardware test"


def test_server_snapshot_isolates_one_unreachable_server(monkeypatch) -> None:
    def probe(ssh: str, *_args: object) -> dict[str, object]:
        if ssh == "compute-a":
            raise RuntimeError("connection timed out")
        return {
            "reachable": True,
            "load1": 0.1,
            "load5": 0.2,
            "load15": 0.3,
            "remote_cores": 128,
            "active_runs": (),
            "active_run_ids": (),
        }

    monkeypatch.setattr(controller_dashboard, "probe_server_state", probe)
    validated = controller_dashboard.validate_payload(
        {
            "schema_version": 1,
            "servers": [
                dashboard_server(),
                dashboard_server(
                    name="compute-b",
                    configured_cores=128,
                    endpoints=[{"ssh": "compute-b", "ssh_profile": "ssh"}],
                ),
            ],
        }
    )

    snapshot = controller_dashboard.collect_server_snapshot(validated, timeout=1)

    assert [item["state"] for item in snapshot] == ["unreachable", "idle"]
    assert snapshot[0]["error"] == "tailscale: connection timed out"


def test_probe_timeout_is_normalized_as_runtime_error(monkeypatch) -> None:
    from remote_runner._internal.controller import dispatcher

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["ssh"], 11)
        ),
    )

    try:
        dispatcher.probe_server_state("compute-a", "/opt/python3", timeout=1)
    except RuntimeError as exc:
        assert "timed out after 11s" in str(exc)
    else:
        raise AssertionError("probe timeout was not normalized")


def test_controller_dashboard_combines_overview_and_server_snapshot(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        controller_service,
        "_read_object",
        lambda _noun: {"schema_version": 1, "servers": [dashboard_server()]},
    )
    monkeypatch.setattr(
        controller_service,
        "status",
        lambda _args: {
            "queue": [],
            "runs": [{"run_id": "rr-0123456789abcdef", "label": "run"}],
            "summary": {},
        },
    )
    monkeypatch.setattr(
        controller_service,
        "collect_server_snapshot",
        lambda _servers, *, timeout: [
            {
                "name": "compute-a",
                "state": "idle",
                "active_runs": [],
                "timeout": timeout,
            }
        ],
    )
    args = argparse.Namespace(timeout=7, interval=60)

    result = controller_service.dashboard(args)

    assert result["queue"] == []
    assert result["servers"][0]["name"] == "compute-a"
    assert result["servers"][0]["timeout"] == 7
    assert result["probe_interval_seconds"] == 60
    assert isinstance(result["collected_at"], str)


def test_controller_dashboard_collects_status_and_snapshot_concurrently(
    monkeypatch,
) -> None:
    started = Barrier(2)
    monkeypatch.setattr(
        controller_service,
        "_read_object",
        lambda _noun: {"schema_version": 1, "servers": [dashboard_server()]},
    )

    def status(_args):
        started.wait(timeout=1)
        return {"queue": [], "runs": [], "summary": {}}

    def snapshot(_servers, *, timeout):
        started.wait(timeout=1)
        assert timeout == 7
        return []

    monkeypatch.setattr(controller_service, "status", status)
    monkeypatch.setattr(controller_service, "collect_server_snapshot", snapshot)

    result = controller_service.dashboard(argparse.Namespace(timeout=7, interval=60))

    assert result["queue"] == []
    assert result["servers"] == []
