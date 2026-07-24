from __future__ import annotations

import argparse
import asyncio

from textual.widgets import DataTable, Static

from remote_runner.tui import (
    RemoteRunnerTui,
    _countdown_seconds,
    _duration,
    _elapsed_seconds,
    _progress_bar,
    _progress_eta,
    _progress_percent,
    _progress_stage,
    _queue_rows,
    _server_rows,
    _server_task_column_width,
)


def test_countdown_ignores_sub_microsecond_float_noise() -> None:
    assert _countdown_seconds(50.0 + 1e-10, 0.0) == 50
    assert _countdown_seconds(50.001, 0.0) == 51
    assert _countdown_seconds(10.0, 11.0) == 0


def test_elapsed_ignores_sub_microsecond_float_noise() -> None:
    started_at = 1016.800718678379
    now = started_at + 10.0
    assert now - started_at < 10.0
    assert _elapsed_seconds(started_at, now) == 10
    assert _elapsed_seconds(10.0, 9.0) == 0


def test_tui_formats_progress_bar() -> None:
    standard = {
        "run_id": "rr-0123456789abcdef",
        "label": "decoder",
        "workload_class": "standard",
        "progress": {"percent": 42.0, "eta_seconds": 1080},
    }
    assert _duration(1080) == "18m00s"
    assert _progress_bar(standard) == "[====------] 42.0% ETA 18m00s"
    assert _progress_eta(standard) == 1080.0


def test_progress_prefers_precise_current_total_over_rounded_percent() -> None:
    run = {
        "progress": {
            "current": 2000,
            "total": 5_000_000,
            "percent": 0.0,
            "eta_seconds": 8_339_701,
        }
    }

    assert _progress_percent(run) == 0.04
    assert _progress_bar(run) == "[----------] 0.04% ETA 96d12h"


def test_progress_stage_uses_scope_and_stage() -> None:
    assert (
        _progress_stage(
            {"progress": {"scope": "lossy_dem_cache", "stage": "precompute"}}
        )
        == "lossy_dem_cache:precompute"
    )


def test_server_table_groups_workloads_without_repeating_server_load() -> None:
    rows = _server_rows(
        [
            {
                "name": "compute-a",
                "state": "busy",
                "load5": 12.0,
                "configured_cores": 256,
                "test_slots": 2,
                "active_runs": [
                    {
                        "run_id": "rr-standard000000",
                        "label": "long standard task label",
                        "workload_class": "standard",
                        "progress": {"percent": 42.0, "eta_seconds": 1080},
                    },
                    {
                        "run_id": "rr-test0000000000",
                        "label": "test task",
                        "workload_class": "test",
                    },
                ],
            }
        ]
    )

    assert len(rows) == 2
    assert rows[0][1] == (
        "compute-a",
        "busy",
        "12.0 / 256",
        "std",
        "long standard task label",
        "[====------] 42.0% ETA 18m00s",
    )
    assert rows[1][1] == (
        "",
        "",
        "",
        "test",
        "test task",
        "[----------] --",
    )


def test_server_task_column_tracks_content_and_available_width() -> None:
    short_rows = [
        ("compute-a:run", ("compute-a", "busy", "8.0 / 256", "std", "train", "[----------] --"))
    ]
    long_rows = [
        (
            "compute-a:run",
            (
                "compute-a",
                "busy",
                "8.0 / 256",
                "std",
                "a very long task label that should not push progress away",
                "[====------] 42.0% ETA 18m00s",
            ),
        )
    ]

    assert _server_task_column_width(short_rows, 120) == len("train")
    assert _server_task_column_width(long_rows, 120) == 32
    assert _server_task_column_width(long_rows, 80) == 8


def test_tui_queue_keeps_unassigned_candidate_servers_visible() -> None:
    rows = _queue_rows(
        {
            "queue": [
                {
                    "job": {
                        "queue_priority": "urgent",
                        "label": "ablation",
                        "workload_class": "standard",
                        "eligible_servers": ["compute-a", "compute-b"],
                    },
                    "state": {"status": "queued"},
                }
            ]
        }
    )

    assert rows == [("urgent", "ablation", "standard", "compute-a, compute-b", "queued")]


def test_textual_app_renders_dashboard_snapshot(monkeypatch) -> None:
    snapshot = {
        "servers": [
            {
                "name": "compute-a",
                "state": "busy",
                "load1": 10.0,
                "load5": 8.0,
                "load15": 6.0,
                "configured_cores": 256,
                "test_slots": 2,
                "active_runs": [
                    {
                        "run_id": "rr-0123456789abcdef",
                        "label": "decoder",
                        "workload_class": "standard",
                    }
                ],
            }
        ],
        "queue": [
            {
                "job": {
                    "label": "waiting",
                    "queue_priority": "normal",
                    "workload_class": "standard",
                    "eligible_servers": ["compute-a"],
                },
                "state": {"status": "queued"},
            }
        ],
    }
    monkeypatch.setattr("remote_runner.tui.query_dashboard", lambda _args: snapshot)

    async def exercise() -> None:
        app = RemoteRunnerTui(argparse.Namespace(), interval=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            servers = app.query_one("#servers", DataTable)
            assert servers.row_count == 1
            assert app.query_one("#queue", DataTable).row_count == 1
            assert "decoder" in str(servers.get_row_at(0))
            assert not app.query("#detail")

    asyncio.run(exercise())


def test_textual_app_refits_task_column_when_terminal_resizes(monkeypatch) -> None:
    snapshot = {
        "servers": [
            {
                "name": "compute-a",
                "state": "busy",
                "load5": 8.0,
                "configured_cores": 256,
                "active_runs": [
                    {
                        "run_id": "rr-0123456789abcdef",
                        "label": "a very long task label that should not push progress away",
                        "workload_class": "standard",
                        "progress": {"percent": 42.0, "eta_seconds": 1080},
                    }
                ],
            }
        ],
        "queue": [],
    }
    monkeypatch.setattr("remote_runner.tui.query_dashboard", lambda _args: snapshot)

    async def exercise() -> None:
        app = RemoteRunnerTui(argparse.Namespace(), interval=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            servers = app.query_one("#servers", DataTable)
            assert servers.ordered_columns[4].width == 32
            assert servers.virtual_size.width <= 120

            await pilot.resize_terminal(80, 25)
            await pilot.pause()
            assert servers.ordered_columns[4].width == 8
            assert servers.virtual_size.width <= 80

    asyncio.run(exercise())


def test_tui_counts_next_probe_from_completed_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        "remote_runner.tui.query_dashboard",
        lambda _args: {"servers": [], "queue": []},
    )

    async def exercise() -> None:
        app = RemoteRunnerTui(argparse.Namespace(), interval=60)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.next_probe_at == app.last_refresh + 60

            app._clock = lambda: app.last_refresh + 10
            app._tick()
            topline = str(app.query_one("#topline", Static).render())
            assert "snapshot age 10s" in topline
            assert "next probe 50s" in topline

            app.probe_in_progress = True
            app._tick()
            topline = str(app.query_one("#topline", Static).render())
            assert "probing controller" in topline
            assert "probe in progress" in topline
            assert "next probe" not in topline

    asyncio.run(exercise())


def test_textual_app_fits_standard_terminal(monkeypatch) -> None:
    monkeypatch.setattr(
        "remote_runner.tui.query_dashboard",
        lambda _args: {"servers": [], "queue": []},
    )

    async def exercise() -> None:
        app = RemoteRunnerTui(argparse.Namespace(), interval=60)
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause()
            assert app.query_one("#topline", Static).region.y == 0
            assert app.query_one("#statusline", Static).region.bottom <= 25
            assert app.query_one("#queue", DataTable).region.height >= 4

    asyncio.run(exercise())
