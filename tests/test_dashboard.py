from __future__ import annotations

import argparse
import json
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
                    "memory_gb": 512,
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
        "machine_id": "compute-a",
        "machine_id_source": "legacy-name",
        "machine_fingerprint": None,
        "enabled": True,
        "auto_select": True,
        "python": "/opt/python3",
        "configured_cores": 256,
        "configured_memory_gb": 512,
        "standard_slots": 1,
        "test_slots": 2,
        "testing_enabled": False,
        "output_root_configured": False,
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
        "machine_id": "compute-a",
        "machine_id_source": "legacy-name",
        "machine_fingerprint": None,
        "enabled": True,
        "auto_select": True,
        "python": "/opt/python3",
        "configured_cores": 256,
        "configured_memory_gb": 512,
        "test_slots": 2,
        "testing_enabled": False,
        "output_root_configured": False,
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
    assert enriched[0]["configured_memory_gb"] == 512
    assert enriched[0]["testing_enabled"] is False
    assert enriched[0]["output_root_configured"] is False
    assert enriched[0]["standard_runs"] == 1
    assert enriched[0]["test_runs"] == 0
    assert enriched[0]["active_runs"][0]["label"] == "decoder sweep"
    assert enriched[0]["active_runs"][0]["progress"] == {"percent": 42.0}
    assert enriched[0]["active_runs"][0]["controller_managed"] is True


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
    assert enriched[0]["active_runs"][0]["controller_managed"] is False


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
                    machine_id="compute-b",
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


def test_probe_server_state_reads_optional_memory_metrics(monkeypatch) -> None:
    from remote_runner._internal.controller import dispatcher

    payload = {
        "active_runs": [],
        "active_run_ids": [],
        "load1": 1.0,
        "load5": 2.0,
        "load15": 3.0,
        "remote_cores": 8,
        "memory_total_bytes": 16 * 1024**3,
        "memory_available_bytes": 6 * 1024**3,
        "memory_used_bytes": 10 * 1024**3,
        "memory_used_percent": 62.5,
    }

    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["ssh"], 0, stdout=json.dumps(payload).encode(), stderr=b""
        ),
    )

    result = dispatcher.probe_server_state("compute-a", "/opt/python3", timeout=1)

    assert result["memory_total_bytes"] == 16 * 1024**3
    assert result["memory_available_bytes"] == 6 * 1024**3
    assert result["memory_used_bytes"] == 10 * 1024**3
    assert result["memory_used_percent"] == 62.5


def test_probe_server_state_rejects_inconsistent_memory_metrics(monkeypatch) -> None:
    from remote_runner._internal.controller import dispatcher

    payload = {
        "active_runs": [],
        "active_run_ids": [],
        "load1": 1.0,
        "load5": 2.0,
        "load15": 3.0,
        "remote_cores": 8,
        "memory_total_bytes": 100,
        "memory_available_bytes": 101,
    }
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["ssh"], 0, stdout=json.dumps(payload).encode(), stderr=b""
        ),
    )

    try:
        dispatcher.probe_server_state("compute-a", "/opt/python3", timeout=1)
    except RuntimeError as exc:
        assert "memory data" in str(exc)
    else:
        raise AssertionError("inconsistent memory metrics should be rejected")


def test_controller_dashboard_combines_overview_and_server_snapshot(
    monkeypatch,
    tmp_path: Path,
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
                "memory_total_bytes": 16 * 1024**3,
                "memory_used_bytes": 10 * 1024**3,
                "memory_used_percent": 62.5,
                "active_runs": [],
                "timeout": timeout,
            }
        ],
    )
    args = argparse.Namespace(
        controller_root=tmp_path / "controller",
        project_id="example",
        timeout=7,
        interval=60,
    )

    result = controller_service.dashboard(args)

    assert result["queue"] == []
    assert result["servers"][0]["name"] == "compute-a"
    assert result["servers"][0]["memory_used_percent"] == 62.5
    assert result["servers"][0]["timeout"] == 7
    assert result["probe_interval_seconds"] == 60
    assert isinstance(result["collected_at"], str)


def test_controller_dashboard_collects_status_and_snapshot_concurrently(
    monkeypatch,
    tmp_path: Path,
) -> None:
    started = Barrier(2)
    monkeypatch.setattr(
        controller_service,
        "_read_object",
        lambda _noun: {"schema_version": 1, "servers": [dashboard_server()]},
    )

    def status(status_args):
        assert status_args._full_overview is True
        started.wait(timeout=1)
        return {"queue": [], "runs": [], "summary": {}}

    def snapshot(_servers, *, timeout):
        started.wait(timeout=1)
        assert timeout == 7
        return []

    monkeypatch.setattr(controller_service, "status", status)
    monkeypatch.setattr(controller_service, "collect_server_snapshot", snapshot)

    result = controller_service.dashboard(
        argparse.Namespace(
            controller_root=tmp_path / "controller",
            project_id="example",
            timeout=7,
            interval=60,
        )
    )

    assert result["queue"] == []
    assert result["servers"] == []
