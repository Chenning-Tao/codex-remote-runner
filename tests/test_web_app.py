from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from remote_runner._internal.queue_control import QueuePreparationError
from remote_runner.web_app import DashboardProbe, InFlightBatchUpdates, create_app


def arguments() -> argparse.Namespace:
    return argparse.Namespace(
        project_config=None,
        server_registry=Path("servers.yaml"),
        timeout=8,
        stop_timeout=12,
    )


def static_root(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><title>Remote Runner</title>", encoding="utf-8"
    )
    return root


def test_identical_in_flight_batch_updates_share_one_operation() -> None:
    updates = InFlightBatchUpdates()
    key = (
        (("rr-0123456789abcdef", 3), ("rr-fedcba9876543210", 8)),
        None,
        None,
        ("compute-a", "compute-b"),
    )
    calls = 0

    async def exercise() -> None:
        nonlocal calls
        started = asyncio.Event()
        release = asyncio.Event()

        async def operation() -> tuple[list[str], list[dict[str, str]]]:
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return ["rr-0123456789abcdef"], []

        first = asyncio.create_task(updates.run(key, operation))
        await started.wait()
        second = asyncio.create_task(updates.run(key, operation))
        await asyncio.sleep(0)
        release.set()

        assert await first == (["rr-0123456789abcdef"], [])
        assert await second == (["rr-0123456789abcdef"], [])

    asyncio.run(exercise())

    assert calls == 1


def test_dashboard_probe_publishes_successful_snapshot() -> None:
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: {"servers": [{"name": "compute-a"}], "queue": []},
    )

    asyncio.run(probe.probe_once())

    document = probe.document()
    assert document["status"] == "online"
    assert document["project_id"] == "example"
    assert document["sequence"] == 2
    assert document["snapshot"] == {
        "servers": [{"name": "compute-a"}],
        "queue": [],
    }
    assert document["refreshed_at"] is not None
    assert document["next_probe_at"] is not None


def test_dashboard_probe_preserves_last_snapshot_after_failure() -> None:
    outcomes: list[object] = [{"servers": [], "queue": []}, RuntimeError("offline")]

    def query(_args: argparse.Namespace) -> dict[str, object]:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, dict)
        return outcome

    probe = DashboardProbe(arguments(), project_id="example", interval=30, query=query)

    async def exercise() -> None:
        await probe.probe_once()
        await probe.probe_once()

    asyncio.run(exercise())

    document = probe.document()
    assert document["status"] == "error"
    assert document["error"] == "offline"
    assert document["snapshot"] == {"servers": [], "queue": []}
    assert document["refreshed_at"] is not None


def test_web_app_serves_snapshot_static_assets_and_security_headers(
    tmp_path: Path,
) -> None:
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: {"servers": [], "queue": []},
    )
    asyncio.run(probe.probe_once())
    app = create_app(probe, static_root=static_root(tmp_path), manage_probe=False)

    with TestClient(app) as client:
        snapshot = client.get("/api/snapshot")
        assert snapshot.status_code == 200
        assert snapshot.json()["status"] == "online"
        assert snapshot.headers["cache-control"] == "no-store"
        assert snapshot.headers["x-frame-options"] == "DENY"
        assert "connect-src 'self'" in snapshot.headers["content-security-policy"]

        page = client.get("/")
        assert page.status_code == 200
        assert "Remote Runner" in page.text

        rejected = client.get("/api/snapshot", headers={"host": "attacker.example"})
        assert rejected.status_code == 400


def test_web_app_requires_built_assets(tmp_path: Path) -> None:
    probe = DashboardProbe(arguments(), project_id="example", interval=30)

    try:
        create_app(probe, static_root=tmp_path)
    except RuntimeError as exc:
        assert "web assets are unavailable" in str(exc)
    else:
        raise AssertionError("missing web assets should prevent startup")


def test_web_stop_requires_explicit_confirmation_and_refreshes_snapshot(
    tmp_path: Path,
) -> None:
    snapshots = iter(
        (
            {"servers": [], "queue": [{"job": {"run_id": "rr-0123456789abcdef"}}]},
            {"servers": [], "queue": []},
        )
    )
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: next(snapshots),
    )
    asyncio.run(probe.probe_once())
    stop_calls: list[argparse.Namespace] = []

    def stop_query(args: argparse.Namespace) -> dict[str, object]:
        stop_calls.append(args)
        return {"kind": "queue", "state": {"status": "stopped"}}

    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        stop_query=stop_query,
    )

    with TestClient(app) as client:
        missing_header = client.post(
            "/api/runs/rr-0123456789abcdef/stop",
            json={"run_id": "rr-0123456789abcdef", "confirm": True},
        )
        assert missing_header.status_code == 403

        invalid_content_type = client.post(
            "/api/runs/rr-0123456789abcdef/stop",
            headers={"x-remote-runner-action": "stop"},
            content="run_id=rr-0123456789abcdef",
        )
        assert invalid_content_type.status_code == 415

        invalid_run_id = client.post(
            "/api/runs/not-a-run/stop",
            headers={"x-remote-runner-action": "stop"},
            json={"run_id": "not-a-run", "confirm": True},
        )
        assert invalid_run_id.status_code == 400

        invalid_confirmation = client.post(
            "/api/runs/rr-0123456789abcdef/stop",
            headers={"x-remote-runner-action": "stop"},
            json={"run_id": "rr-0123456789abcdef", "confirm": False},
        )
        assert invalid_confirmation.status_code == 400

        stopped = client.post(
            "/api/runs/rr-0123456789abcdef/stop",
            headers={"x-remote-runner-action": "stop"},
            json={"run_id": "rr-0123456789abcdef", "confirm": True},
        )

        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"
        assert probe.document()["snapshot"] == {"servers": [], "queue": []}
        assert len(stop_calls) == 1
        assert stop_calls[0].run_id == "rr-0123456789abcdef"
        assert stop_calls[0].timeout == 12


def test_web_stop_reports_controller_failure_without_refreshing(
    tmp_path: Path,
) -> None:
    query_calls = 0

    def query(_args: argparse.Namespace) -> dict[str, object]:
        nonlocal query_calls
        query_calls += 1
        return {"servers": [], "queue": []}

    probe = DashboardProbe(arguments(), project_id="example", interval=30, query=query)
    asyncio.run(probe.probe_once())
    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        stop_query=lambda _args: (_ for _ in ()).throw(
            RuntimeError("stop outcome unknown")
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/runs/rr-0123456789abcdef/stop",
            headers={"x-remote-runner-action": "stop"},
            json={"run_id": "rr-0123456789abcdef", "confirm": True},
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": "stop_failed",
        "detail": "stop outcome unknown",
    }
    assert query_calls == 1


def test_web_stop_refreshes_stale_snapshot_when_run_no_longer_exists(
    tmp_path: Path,
) -> None:
    snapshots = iter(
        (
            {"servers": [], "queue": [{"job": {"run_id": "rr-0123456789abcdef"}}]},
            {"servers": [], "queue": []},
        )
    )
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: next(snapshots),
    )
    asyncio.run(probe.probe_once())
    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        stop_query=lambda _args: (_ for _ in ()).throw(
            RuntimeError(
                "usage: __main__.py [-h] ...\n"
                "__main__.py: error: controller run does not exist: "
                "rr-0123456789abcdef"
            )
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/runs/rr-0123456789abcdef/stop",
            headers={"x-remote-runner-action": "stop"},
            json={"run_id": "rr-0123456789abcdef", "confirm": True},
        )

    assert response.status_code == 404
    assert response.json() == {"error": "run_not_found"}
    assert response.headers["cache-control"] == "no-store"
    assert probe.document()["snapshot"] == {"servers": [], "queue": []}


def test_web_queue_update_requires_revision_and_refreshes_snapshot_in_background(
    tmp_path: Path,
) -> None:
    snapshots = iter(
        (
            {
                "servers": [],
                "queue": [
                    {
                        "job": {
                            "run_id": "rr-0123456789abcdef",
                            "queue_priority": "normal",
                        },
                        "state": {"status": "queued", "revision": 3},
                    }
                ],
            },
            {
                "servers": [],
                "queue": [
                    {
                        "job": {
                            "run_id": "rr-0123456789abcdef",
                            "queue_priority": "urgent",
                        },
                        "state": {"status": "queued", "revision": 4},
                    }
                ],
            },
        )
    )
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: next(snapshots),
    )
    asyncio.run(probe.probe_once())
    calls: list[tuple[str, dict[str, object]]] = []

    def update_query(
        _args: argparse.Namespace,
        run_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        calls.append((run_id, payload))
        return {"changed": True}

    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        queue_update_query=update_query,
    )

    with TestClient(app) as client:
        rejected = client.patch(
            "/api/queue/rr-0123456789abcdef",
            json={
                "run_id": "rr-0123456789abcdef",
                "expected_revision": 3,
                "queue_priority": "urgent",
            },
        )
        assert rejected.status_code == 403

        response = client.patch(
            "/api/queue/rr-0123456789abcdef",
            headers={"x-remote-runner-action": "update-queue"},
            json={
                "run_id": "rr-0123456789abcdef",
                "expected_revision": 3,
                "queue_priority": "urgent",
                "workload_class": "test",
                "eligible_servers": ["compute-b"],
                "move": "first",
            },
        )

    assert response.status_code == 200
    assert calls == [
        (
            "rr-0123456789abcdef",
            {
                "expected_revision": 3,
                "queue_priority": "urgent",
                "workload_class": "test",
                "eligible_servers": ["compute-b"],
                "move": "first",
            },
        )
    ]
    assert probe.document()["snapshot"]["queue"][0]["job"]["queue_priority"] == "urgent"


def test_web_queue_update_sends_response_before_background_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: {"servers": [], "queue": []},
    )
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def slow_refresh() -> None:
        refresh_started.set()
        await release_refresh.wait()

    monkeypatch.setattr(probe, "probe_once", slow_refresh)
    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        queue_update_query=lambda *_args: {"changed": True},
    )
    payload = json.dumps(
        {
            "run_id": "rr-0123456789abcdef",
            "expected_revision": 3,
            "move": "first",
        }
    ).encode()

    async def exercise() -> None:
        request_sent = False
        response_sent = asyncio.Event()
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)
            if message["type"] == "http.response.body":
                response_sent.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "PATCH",
            "scheme": "http",
            "path": "/api/queue/rr-0123456789abcdef",
            "raw_path": b"/api/queue/rr-0123456789abcdef",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"x-remote-runner-action", b"update-queue"),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        request = asyncio.create_task(app(scope, receive, send))
        await response_sent.wait()
        await refresh_started.wait()

        assert not request.done()
        assert messages[0]["type"] == "http.response.start"
        assert messages[0]["status"] == 200
        assert messages[1]["type"] == "http.response.body"

        release_refresh.set()
        await request

    asyncio.run(exercise())


def test_web_capacity_update_requires_revision_and_refreshes_snapshot(
    tmp_path: Path,
) -> None:
    snapshots = iter(
        (
            {
                "servers": [
                    {
                        "name": "compute-a",
                        "standard_slots": 1,
                        "test_slots": 1,
                        "capacity_revision": 2,
                    }
                ],
                "queue": [],
            },
            {
                "servers": [
                    {
                        "name": "compute-a",
                        "standard_slots": 3,
                        "test_slots": 4,
                        "capacity_revision": 3,
                    }
                ],
                "queue": [],
            },
        )
    )
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: next(snapshots),
    )
    asyncio.run(probe.probe_once())
    calls: list[tuple[str, dict[str, object]]] = []

    def update_query(
        _args: argparse.Namespace,
        server: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        calls.append((server, payload))
        return {"changed": True}

    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        capacity_update_query=update_query,
    )
    request = {
        "server": "compute-a",
        "expected_revision": 2,
        "standard_slots": 3,
        "test_slots": 4,
    }

    with TestClient(app) as client:
        rejected = client.patch("/api/servers/compute-a/capacity", json=request)
        assert rejected.status_code == 403
        response = client.patch(
            "/api/servers/compute-a/capacity",
            headers={"x-remote-runner-action": "update-capacity"},
            json=request,
        )

    assert response.status_code == 200
    assert calls == [
        (
            "compute-a",
            {"expected_revision": 2, "standard_slots": 3, "test_slots": 4},
        )
    ]
    server = probe.document()["snapshot"]["servers"][0]
    assert server["standard_slots"] == 3
    assert server["capacity_revision"] == 3


def test_web_server_drain_and_resume_require_confirmation_and_refresh_snapshot(
    tmp_path: Path,
) -> None:
    snapshots = iter(
        (
            {
                "servers": [{"name": "compute-a"}],
                "queue": [],
                "server_drains": {"scope": "controller", "servers": {}},
            },
            {
                "servers": [{"name": "compute-a"}],
                "queue": [],
                "server_drains": {
                    "scope": "controller",
                    "servers": {"compute-a": {"requested_by_project": "example"}},
                },
            },
            {
                "servers": [{"name": "compute-a"}],
                "queue": [],
                "server_drains": {"scope": "controller", "servers": {}},
            },
        )
    )
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: next(snapshots),
    )
    asyncio.run(probe.probe_once())
    calls: list[tuple[str, bool]] = []

    def drain_query(
        _args: argparse.Namespace,
        server: str,
        drained: bool,
    ) -> dict[str, object]:
        calls.append((server, drained))
        return {"changed": True, "server": server, "drained": drained}

    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        server_drain_query=drain_query,
    )
    request = {"server": "compute-a", "confirm": True}

    with TestClient(app) as client:
        rejected = client.post("/api/servers/compute-a/drain", json=request)
        assert rejected.status_code == 403
        invalid_confirmation = client.post(
            "/api/servers/compute-a/drain",
            headers={"x-remote-runner-action": "drain-server"},
            json={"server": "compute-a", "confirm": False},
        )
        assert invalid_confirmation.status_code == 400
        drained = client.post(
            "/api/servers/compute-a/drain",
            headers={"x-remote-runner-action": "drain-server"},
            json=request,
        )
        resumed = client.post(
            "/api/servers/compute-a/resume",
            headers={"x-remote-runner-action": "resume-server"},
            json=request,
        )

    assert drained.status_code == 200
    assert drained.json()["status"] == "drained"
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "resumed"
    assert calls == [("compute-a", True), ("compute-a", False)]
    assert probe.document()["snapshot"]["server_drains"]["servers"] == {}


def test_web_server_retirement_requires_confirmation_and_refreshes_snapshot(
    tmp_path: Path,
) -> None:
    snapshots = iter(
        (
            {
                "servers": [{"name": "compute-a", "enabled": True}],
                "queue": [],
                "server_drains": {"scope": "controller", "servers": {}},
            },
            {
                "servers": [{"name": "compute-a", "enabled": False}],
                "queue": [],
                "server_drains": {
                    "scope": "controller",
                    "servers": {"compute-a": {"requested_by_project": "example"}},
                },
            },
        )
    )
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: next(snapshots),
    )
    asyncio.run(probe.probe_once())
    calls: list[str] = []
    preview_calls: list[str] = []

    def retirement_query(
        _args: argparse.Namespace,
        server: str,
    ) -> dict[str, object]:
        calls.append(server)
        return {"status": "retired", "server": server}

    def retirement_preview(
        _args: argparse.Namespace,
        server: str,
    ) -> dict[str, object]:
        preview_calls.append(server)
        return {
            "schema_version": 2,
            "server": server,
            "ready": True,
            "status": "ready_to_retire",
            "assessment": {"effective_blockers": [], "attention": []},
            "cleanup": {"project_config": {}},
        }

    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        server_retirement_query=retirement_query,
        server_retirement_preview=retirement_preview,
    )
    request = {"server": "compute-a", "confirm": True}

    with TestClient(app) as client:
        preview = client.get("/api/servers/compute-a/retirement")
        assert preview.status_code == 200
        assert preview.json()["ready"] is True
        rejected = client.post("/api/servers/compute-a/retire", json=request)
        assert rejected.status_code == 403
        invalid_confirmation = client.post(
            "/api/servers/compute-a/retire",
            headers={"x-remote-runner-action": "retire-server"},
            json={"server": "compute-a", "confirm": False},
        )
        assert invalid_confirmation.status_code == 400
        retired = client.post(
            "/api/servers/compute-a/retire",
            headers={"x-remote-runner-action": "retire-server"},
            json=request,
        )

    assert retired.status_code == 200
    assert retired.json()["status"] == "retired"
    assert calls == ["compute-a"]
    assert preview_calls == ["compute-a"]
    snapshot = probe.document()["snapshot"]
    assert snapshot["servers"][0]["enabled"] is False
    assert "compute-a" in snapshot["server_drains"]["servers"]


def test_web_server_retirement_reports_preflight_blocker(tmp_path: Path) -> None:
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: {
            "servers": [{"name": "archive", "enabled": True}],
            "queue": [],
        },
    )
    asyncio.run(probe.probe_once())

    def blocked(_args: argparse.Namespace, server: str) -> dict[str, object]:
        raise ValueError(
            f"cannot retire output synchronization target {server!r}; "
            "move output_sync.target_server first"
        )

    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        server_retirement_query=blocked,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/servers/archive/retire",
            headers={"x-remote-runner-action": "retire-server"},
            json={"server": "archive", "confirm": True},
        )

    assert response.status_code == 409
    assert response.json()["error"] == "server_retirement_blocked"
    assert "move output_sync.target_server first" in response.json()["detail"]


def test_web_batch_queue_update_reports_partial_results_and_refreshes_snapshot(
    tmp_path: Path,
) -> None:
    first_run = "rr-0123456789abcdef"
    second_run = "rr-fedcba9876543210"
    snapshots = iter(
        (
            {
                "servers": [],
                "queue": [
                    {"job": {"run_id": first_run}},
                    {"job": {"run_id": second_run}},
                ],
            },
            {"servers": [], "queue": [{"job": {"run_id": second_run}}]},
        )
    )
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: next(snapshots),
    )
    asyncio.run(probe.probe_once())
    calls: list[tuple[str, dict[str, object]]] = []

    def update_query(
        _args: argparse.Namespace,
        run_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        calls.append((run_id, payload))
        if run_id == second_run:
            raise RuntimeError("queued state revision conflict")
        return {"changed": True}

    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        queue_update_query=update_query,
    )
    request = {
        "updates": [
            {"run_id": first_run, "expected_revision": 3},
            {"run_id": second_run, "expected_revision": 8},
        ],
        "eligible_servers": ["compute-a", "compute-b"],
    }

    with TestClient(app) as client:
        rejected = client.patch("/api/queue-batch", json=request)
        assert rejected.status_code == 403
        response = client.patch(
            "/api/queue-batch",
            headers={"x-remote-runner-action": "update-queue-batch"},
            json=request,
        )

    assert response.status_code == 207
    assert response.json() == {
        "status": "partial",
        "succeeded": [first_run],
        "failed": [
            {
                "run_id": second_run,
                "error": "queue_conflict",
                "detail": "queued state revision conflict",
            }
        ],
    }
    assert calls == [
        (
            first_run,
            {
                "expected_revision": 3,
                "eligible_servers": ["compute-a", "compute-b"],
            },
        ),
        (
            second_run,
            {
                "expected_revision": 8,
                "eligible_servers": ["compute-a", "compute-b"],
            },
        ),
    ]
    assert probe.document()["snapshot"] == {
        "servers": [],
        "queue": [{"job": {"run_id": second_run}}],
    }


def test_web_batch_queue_update_rejects_duplicate_runs(tmp_path: Path) -> None:
    run_id = "rr-0123456789abcdef"
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: {"servers": [], "queue": []},
    )
    asyncio.run(probe.probe_once())
    app = create_app(probe, static_root=static_root(tmp_path), manage_probe=False)

    with TestClient(app) as client:
        response = client.patch(
            "/api/queue-batch",
            headers={"x-remote-runner-action": "update-queue-batch"},
            json={
                "updates": [
                    {"run_id": run_id, "expected_revision": 1},
                    {"run_id": run_id, "expected_revision": 1},
                ],
                "eligible_servers": ["compute-a"],
            },
        )

    assert response.status_code == 400
    assert response.json() == {"error": "batch queue update contains duplicate runs"}


def test_web_batch_queue_update_applies_priority_and_tracks_revision_bumps(
    tmp_path: Path,
) -> None:
    run_ids = [
        "rr-0123456789abcdef",
        "rr-fedcba9876543210",
        "rr-0011223344556677",
    ]
    revisions = [3, 8, 12]
    priorities = ["normal", "urgent", "normal"]
    snapshot = {
        "servers": [],
        "queue": [
            {
                "job": {"run_id": run_id, "queue_priority": priority},
                "state": {"status": "queued", "revision": revision},
            }
            for run_id, revision, priority in zip(
                run_ids, revisions, priorities, strict=True
            )
        ],
    }
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: snapshot,
    )
    asyncio.run(probe.probe_once())
    calls: list[tuple[str, dict[str, object]]] = []

    def update_query(
        _args: argparse.Namespace,
        run_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        calls.append((run_id, payload))
        return {"changed": True}

    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        queue_update_query=update_query,
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/queue-batch",
            headers={"x-remote-runner-action": "update-queue-batch"},
            json={
                "updates": [
                    {"run_id": run_id, "expected_revision": revision}
                    for run_id, revision in zip(run_ids, revisions, strict=True)
                ],
                "queue_priority": "urgent",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "updated",
        "succeeded": run_ids,
        "failed": [],
    }
    assert calls == [
        (run_ids[0], {"expected_revision": 3, "queue_priority": "urgent"}),
        (run_ids[1], {"expected_revision": 9, "queue_priority": "urgent"}),
        (run_ids[2], {"expected_revision": 13, "queue_priority": "urgent"}),
    ]


def test_web_batch_queue_update_requires_at_least_one_setting(tmp_path: Path) -> None:
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: {"servers": [], "queue": []},
    )
    app = create_app(probe, static_root=static_root(tmp_path), manage_probe=False)

    with TestClient(app) as client:
        response = client.patch(
            "/api/queue-batch",
            headers={"x-remote-runner-action": "update-queue-batch"},
            json={
                "updates": [
                    {
                        "run_id": "rr-0123456789abcdef",
                        "expected_revision": 1,
                    }
                ]
            },
        )

    assert response.status_code == 400
    assert response.json() == {"error": "batch queue update request is invalid"}


def test_web_queue_update_reports_conflict_and_refreshes_snapshot(
    tmp_path: Path,
) -> None:
    snapshots = iter(
        (
            {"servers": [], "queue": [{"job": {"run_id": "rr-0123456789abcdef"}}]},
            {"servers": [], "queue": []},
        )
    )
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: next(snapshots),
    )
    asyncio.run(probe.probe_once())
    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        queue_update_query=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("queued state revision conflict")
        ),
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/queue/rr-0123456789abcdef",
            headers={"x-remote-runner-action": "update-queue"},
            json={
                "run_id": "rr-0123456789abcdef",
                "expected_revision": 2,
                "move": "up",
            },
        )

    assert response.status_code == 409
    assert response.json() == {"error": "queue_conflict"}
    assert probe.document()["snapshot"] == {"servers": [], "queue": []}


def test_web_queue_update_reports_server_preparation_failure(
    tmp_path: Path,
) -> None:
    snapshots = iter(
        (
            {"servers": [], "queue": [{"job": {"run_id": "rr-0123456789abcdef"}}]},
            {"servers": [], "queue": [{"job": {"run_id": "rr-0123456789abcdef"}}]},
        )
    )
    probe = DashboardProbe(
        arguments(),
        project_id="example",
        interval=30,
        query=lambda _args: next(snapshots),
    )
    asyncio.run(probe.probe_once())
    app = create_app(
        probe,
        static_root=static_root(tmp_path),
        manage_probe=False,
        queue_update_query=lambda *_args: (_ for _ in ()).throw(
            QueuePreparationError("compute-b: push failed")
        ),
    )

    with TestClient(app) as client:
        response = client.patch(
            "/api/queue/rr-0123456789abcdef",
            headers={"x-remote-runner-action": "update-queue"},
            json={
                "run_id": "rr-0123456789abcdef",
                "expected_revision": 2,
                "eligible_servers": ["compute-b"],
            },
        )

    assert response.status_code == 409
    assert response.json() == {
        "error": "queue_preparation_failed",
        "detail": "compute-b: push failed",
    }
