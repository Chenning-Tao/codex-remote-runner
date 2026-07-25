from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from remote_runner.web_app import DashboardProbe, create_app


def arguments() -> argparse.Namespace:
    return argparse.Namespace(
        project_config=None,
        server_registry=Path("servers.yaml"),
        timeout=8,
    )


def static_root(tmp_path: Path) -> Path:
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><title>Remote Runner</title>", encoding="utf-8"
    )
    return root


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

    probe = DashboardProbe(
        arguments(), project_id="example", interval=30, query=query
    )

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

        rejected = client.get(
            "/api/snapshot", headers={"host": "attacker.example"}
        )
        assert rejected.status_code == 400


def test_web_app_requires_built_assets(tmp_path: Path) -> None:
    probe = DashboardProbe(arguments(), project_id="example", interval=30)

    try:
        create_app(probe, static_root=tmp_path)
    except RuntimeError as exc:
        assert "web assets are unavailable" in str(exc)
    else:
        raise AssertionError("missing web assets should prevent startup")
