from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass
from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.events import Resize
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Button, DataTable, Footer, Static

from ._internal.config import load_managed_project_config
from ._internal.dashboard import query_dashboard
from ._internal.execution_registry import resolve_project_config
from ._internal.stopping import request_stop


PROGRESS_BAR_WIDTH = 10
SERVER_COLUMN_LABELS = (
    "Server",
    "State",
    "Load1 / cores",
    "Lane",
    "Task",
    "Progress",
)
SERVER_TASK_COLUMN_INDEX = 4
SERVER_TASK_MAX_WIDTH = 32
DISPLAY_TIME_EPSILON_SECONDS = 1e-6


@dataclass(frozen=True)
class StopTarget:
    run_id: str
    label: str
    location: str


def _progress_percent(run: dict[str, Any]) -> float | None:
    progress = run.get("progress")
    if not isinstance(progress, dict):
        return None
    current = progress.get("current")
    total = progress.get("total")
    if (
        isinstance(current, (int, float))
        and not isinstance(current, bool)
        and isinstance(total, (int, float))
        and not isinstance(total, bool)
        and math.isfinite(float(current))
        and math.isfinite(float(total))
        and float(total) > 0
    ):
        return min(100.0, max(0.0, 100.0 * float(current) / float(total)))
    percent = progress.get("percent")
    if (
        isinstance(percent, (int, float))
        and not isinstance(percent, bool)
        and math.isfinite(float(percent))
    ):
        return min(100.0, max(0.0, float(percent)))
    return None


def _progress_eta(run: dict[str, Any]) -> float | None:
    progress = run.get("progress")
    if not isinstance(progress, dict):
        return None
    eta = progress.get("eta_seconds")
    if (
        not isinstance(eta, (int, float))
        or isinstance(eta, bool)
        or not math.isfinite(float(eta))
        or float(eta) < 0.0
    ):
        return None
    return float(eta)


def _progress_stage(run: dict[str, Any]) -> str | None:
    progress = run.get("progress")
    if not isinstance(progress, dict):
        return None
    scope = progress.get("scope")
    stage = progress.get("stage")
    if isinstance(scope, str) and isinstance(stage, str):
        return f"{scope}:{stage}"
    return stage if isinstance(stage, str) else None


def _percent_text(percent: float) -> str:
    if 0 < percent < 0.1:
        return f"{percent:.2f}%"
    return f"{percent:.1f}%"


def _progress_bar(run: dict[str, Any], width: int = PROGRESS_BAR_WIDTH) -> str:
    percent = _progress_percent(run)
    if percent is None:
        text = f"[{'-' * width}] --"
    else:
        filled = min(width, max(0, int(percent * width / 100.0)))
        text = f"[{'=' * filled}{'-' * (width - filled)}] {_percent_text(percent)}"
    eta = _progress_eta(run)
    return f"{text} ETA {_duration(eta)}" if eta is not None else text


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _countdown_seconds(deadline: float, now: float) -> int:
    remaining = deadline - now
    return max(0, math.ceil(remaining - DISPLAY_TIME_EPSILON_SECONDS))


def _elapsed_seconds(started_at: float, now: float) -> int:
    elapsed = now - started_at
    return max(0, math.floor(elapsed + DISPLAY_TIME_EPSILON_SECONDS))


def _run_label(run: dict[str, Any]) -> str:
    return str(run.get("label") or run.get("run_id") or "unknown")


def _load_text(server: dict[str, Any]) -> str:
    load1 = server.get("load1")
    cores = server.get("configured_cores")
    if not isinstance(load1, (int, float)) or isinstance(load1, bool):
        return "--"
    return f"{float(load1):.1f} / {cores if cores is not None else '--'}"


def _server_rows(servers: list[object]) -> list[tuple[str, tuple[str, ...]]]:
    rows: list[tuple[str, tuple[str, ...]]] = []
    for raw in servers:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "unknown"))
        state = str(raw.get("state", "unknown"))
        load = _load_text(raw)
        raw_active_runs = raw.get("active_runs", [])
        if not isinstance(raw_active_runs, list):
            raw_active_runs = []
        active_runs = sorted(
            (run for run in raw_active_runs if isinstance(run, dict)),
            key=lambda run: run.get("workload_class") == "test",
        )
        if not active_runs:
            task = "available" if state == "idle" else str(raw.get("error") or "--")
            rows.append(
                (
                    f"{name}:idle",
                    (name, state, load, "--", task, _progress_bar({})),
                )
            )
            continue
        for index, run in enumerate(active_runs):
            workload_class = str(run.get("workload_class", "standard"))
            lane = "std" if workload_class == "standard" else "test"
            run_id = str(run.get("run_id", index))
            task = _run_label(run)
            stage = _progress_stage(run)
            if stage is not None:
                task = f"{task} | {stage}"
            server_cells = (name, state, load) if index == 0 else ("", "", "")
            rows.append(
                (
                    f"{name}:{run_id}",
                    (
                        *server_cells,
                        lane,
                        task,
                        _progress_bar(run),
                    ),
                )
            )
    return rows


def _server_task_column_width(
    rows: list[tuple[str, tuple[str, ...]]],
    table_width: int,
    *,
    cell_padding: int = 1,
) -> int:
    """Fit Task to its content without letting it push Progress too far away."""
    content_widths = [cell_len(label) for label in SERVER_COLUMN_LABELS]
    for _, cells in rows:
        for index, cell in enumerate(cells):
            content_widths[index] = max(content_widths[index], cell_len(cell))

    other_columns_width = sum(
        width + 2 * cell_padding
        for index, width in enumerate(content_widths)
        if index != SERVER_TASK_COLUMN_INDEX
    )
    available_width = max(
        cell_len(SERVER_COLUMN_LABELS[SERVER_TASK_COLUMN_INDEX]),
        table_width - other_columns_width - 2 * cell_padding,
    )
    return min(
        content_widths[SERVER_TASK_COLUMN_INDEX],
        SERVER_TASK_MAX_WIDTH,
        available_width,
    )


def _queue_rows(
    snapshot: dict[str, Any],
) -> list[tuple[str, tuple[str, ...]]]:
    rows: list[tuple[str, tuple[str, ...]]] = []
    for index, item in enumerate(snapshot.get("queue", [])):
        if not isinstance(item, dict):
            continue
        job = item.get("job")
        state = item.get("state")
        if not isinstance(job, dict) or not isinstance(state, dict):
            continue
        eligible = job.get("eligible_servers", [])
        eligible_text = ", ".join(str(name) for name in eligible)
        run_id = str(job.get("run_id") or index)
        rows.append(
            (
                f"queue:{run_id}",
                (
                    str(job.get("queue_priority", "normal")),
                    str(job.get("label", job.get("run_id", "unknown"))),
                    str(job.get("workload_class", "standard")),
                    eligible_text,
                    str(state.get("status", "unknown")),
                ),
            )
        )
    return rows


def _stop_targets(snapshot: dict[str, Any]) -> dict[tuple[str, str], StopTarget]:
    targets: dict[tuple[str, str], StopTarget] = {}
    raw_queue = snapshot.get("queue", [])
    if isinstance(raw_queue, list):
        for item in raw_queue:
            if not isinstance(item, dict):
                continue
            job = item.get("job")
            state = item.get("state")
            if not isinstance(job, dict) or not isinstance(state, dict):
                continue
            run_id = job.get("run_id")
            status = state.get("status")
            if not isinstance(run_id, str) or status not in {"queued", "dispatching"}:
                continue
            targets[("queue", f"queue:{run_id}")] = StopTarget(
                run_id=run_id,
                label=str(job.get("label") or run_id),
                location=f"queue ({status})",
            )

    raw_servers = snapshot.get("servers", [])
    if isinstance(raw_servers, list):
        for server in raw_servers:
            if not isinstance(server, dict):
                continue
            name = str(server.get("name", "unknown"))
            active_runs = server.get("active_runs", [])
            if not isinstance(active_runs, list):
                continue
            for run in active_runs:
                if not isinstance(run, dict):
                    continue
                run_id = run.get("run_id")
                controller_managed = run.get("controller_managed")
                if controller_managed is None:
                    controller_managed = isinstance(
                        run.get("authoritative_status"), str
                    )
                if not isinstance(run_id, str) or controller_managed is not True:
                    continue
                targets[("servers", f"{name}:{run_id}")] = StopTarget(
                    run_id=run_id,
                    label=_run_label(run),
                    location=f"server {name}",
                )
    return targets


class ServerDataTable(DataTable):
    """Server table whose Task column follows both content and viewport width."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.task_column_key: Any = None
        self.server_rows: list[tuple[str, tuple[str, ...]]] = []

    def fit_task_column(self) -> None:
        if self.task_column_key is None:
            return
        column = self.columns[self.task_column_key]
        width = _server_task_column_width(
            self.server_rows,
            self.size.width,
            cell_padding=self.cell_padding,
        )
        if column.width == width:
            return
        column.width = width
        self._require_update_dimensions = True
        self.check_idle()
        self.refresh()

    def on_resize(self, event: Resize) -> None:
        if self.task_column_key is not None:
            self.call_after_refresh(self.fit_task_column)


class ConfirmStopScreen(ModalScreen[StopTarget | None]):
    BINDINGS = [Binding("escape", "cancel", show=False)]
    CSS = """
    ConfirmStopScreen {
        align: center middle;
        background: $background 70%;
    }

    #stop-dialog {
        width: 68;
        max-width: 92%;
        height: auto;
        padding: 1 2;
        border: solid $error;
        background: $surface;
    }

    #stop-title {
        height: 1;
        text-style: bold;
        color: $error;
    }

    #stop-detail {
        height: auto;
        margin: 1 0;
    }

    #stop-actions {
        height: 3;
        align: right middle;
    }

    #stop-actions Button {
        margin-left: 1;
    }
    """

    def __init__(self, target: StopTarget) -> None:
        super().__init__()
        self.target = target

    def compose(self) -> ComposeResult:
        with Vertical(id="stop-dialog"):
            yield Static("Stop workload?", id="stop-title")
            yield Static(
                f"{self.target.label}\n{self.target.run_id}\n{self.target.location}",
                id="stop-detail",
            )
            with Horizontal(id="stop-actions"):
                yield Button("Cancel", id="cancel-stop")
                yield Button("Stop", id="confirm-stop", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel-stop", Button).focus()

    @on(Button.Pressed, "#cancel-stop")
    def cancel_stop(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#confirm-stop")
    def confirm_stop(self) -> None:
        self.dismiss(self.target)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RemoteRunnerTui(App[None]):
    TITLE = "Remote Runner"
    BINDINGS = [
        Binding("x", "stop_selected", "Stop"),
        Binding("q", "quit", "Quit"),
    ]
    CSS = """
    Screen {
        layout: vertical;
    }

    #topline, #statusline {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text;
    }

    #statusline {
        color: $text-muted;
    }

    .section-label {
        height: 1;
        padding: 0 1;
        text-style: bold;
    }

    #servers {
        height: 2fr;
        min-height: 6;
    }

    #queue {
        height: 1fr;
        min-height: 4;
    }
    """

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        interval: int,
        stop_timeout: int = 10,
    ) -> None:
        super().__init__()
        self.args = args
        self.interval = interval
        self.stop_timeout = stop_timeout
        self.snapshot: dict[str, Any] | None = None
        self.stop_targets: dict[tuple[str, str], StopTarget] = {}
        self.last_refresh = 0.0
        self.next_probe_at = 0.0
        self.probe_in_progress = False
        self.stop_in_progress = False
        self.action_message: str | None = None
        self.last_error: str | None = None
        self.probe_timer: Timer | None = None
        self._clock = time.monotonic

    def compose(self) -> ComposeResult:
        yield Static("REMOTE RUNNER | connecting to controller", id="topline")
        yield Static("SERVERS", classes="section-label")
        yield ServerDataTable(id="servers", cursor_type="row", zebra_stripes=True)
        yield Static("UNASSIGNED QUEUE", classes="section-label")
        yield DataTable(id="queue", cursor_type="row", zebra_stripes=True)
        yield Static("Waiting for the first snapshot.", id="statusline")
        yield Footer()

    def on_mount(self) -> None:
        servers = self.query_one("#servers", ServerDataTable)
        for index, label in enumerate(SERVER_COLUMN_LABELS):
            key = servers.add_column(
                label,
                width=cell_len(label) if index == SERVER_TASK_COLUMN_INDEX else None,
            )
            if index == SERVER_TASK_COLUMN_INDEX:
                servers.task_column_key = key
        queue = self.query_one("#queue", DataTable)
        queue.add_columns("Priority", "Task", "Class", "Eligible servers", "State")
        self.set_interval(1, self._tick)
        self._start_probe()

    def _start_probe(self) -> None:
        if self.probe_timer is not None:
            self.probe_timer.stop()
            self.probe_timer = None
        self.probe_in_progress = True
        self.next_probe_at = 0.0
        self.last_error = None
        self._tick()
        self.refresh_snapshot()

    def _schedule_probe(self) -> None:
        if self.probe_timer is not None:
            self.probe_timer.stop()
        self.probe_timer = self.set_timer(self.interval, self._start_probe)

    @work(thread=True, group="refresh", exclusive=True)
    def refresh_snapshot(self) -> None:
        try:
            snapshot = query_dashboard(self.args)
        except (OSError, RuntimeError, ValueError) as exc:
            self.call_from_thread(self._show_error, str(exc))
            return
        self.call_from_thread(self._apply_snapshot, snapshot)

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        now = self._clock()
        self.snapshot = snapshot
        self.last_refresh = now
        self.next_probe_at = now + self.interval
        self.probe_in_progress = False
        self.last_error = None
        self._schedule_probe()
        self.stop_targets = _stop_targets(snapshot)

        table = self.query_one("#servers", ServerDataTable)
        table.clear(columns=False)
        servers = snapshot.get("servers", [])
        if not isinstance(servers, list):
            servers = []
        server_rows = _server_rows(servers)
        table.server_rows = server_rows
        for key, cells in server_rows:
            table.add_row(
                *(
                    Text(cell, overflow="ellipsis", no_wrap=True)
                    if index == SERVER_TASK_COLUMN_INDEX
                    else Text(cell)
                    for index, cell in enumerate(cells)
                ),
                key=key,
            )
        table.fit_task_column()

        queue = self.query_one("#queue", DataTable)
        queue.clear(columns=False)
        for key, cells in _queue_rows(snapshot):
            queue.add_row(*(Text(value) for value in cells), key=key)
        self._tick()

    def _show_error(self, error: str) -> None:
        now = self._clock()
        self.next_probe_at = now + self.interval
        self.probe_in_progress = False
        self.last_error = error
        self._schedule_probe()
        self._tick()

    def action_stop_selected(self) -> None:
        if self.stop_in_progress:
            self.notify("A stop request is already in progress.", severity="warning")
            return
        if isinstance(self.screen, ConfirmStopScreen):
            return
        table = self.focused
        if not isinstance(table, DataTable) or table.id not in {"servers", "queue"}:
            self.notify(
                "Select a running or queued workload first.", severity="warning"
            )
            return
        if table.row_count == 0:
            self.notify("There is no workload on the selected row.", severity="warning")
            return
        cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
        target = self.stop_targets.get((table.id, str(cell_key.row_key.value)))
        if target is None:
            self.notify("The selected row cannot be stopped.", severity="warning")
            return
        self.push_screen(
            ConfirmStopScreen(target),
            self._stop_confirmed,
        )

    def _stop_confirmed(self, target: StopTarget | None) -> None:
        if target is None:
            return
        self.stop_in_progress = True
        self.action_message = f"stopping {target.label}"
        self._tick()
        self.stop_workload(target)

    @work(thread=True, group="stop", exclusive=True)
    def stop_workload(self, target: StopTarget) -> None:
        stop_args = argparse.Namespace(
            project_config=self.args.project_config,
            run_id=target.run_id,
            timeout=self.stop_timeout,
        )
        try:
            result = request_stop(stop_args)
        except (OSError, RuntimeError, ValueError) as exc:
            self.call_from_thread(self._stop_failed, target, str(exc))
            return
        self.call_from_thread(self._stop_succeeded, target, result)

    def _stop_succeeded(
        self,
        target: StopTarget,
        result: dict[str, Any],
    ) -> None:
        kind = str(result.get("kind", "workload"))
        state = result.get("state")
        status = state.get("status") if isinstance(state, dict) else None
        self.stop_in_progress = False
        if status == "stopped":
            self.action_message = f"stopped {target.label} ({kind})"
            self.notify(f"Stopped {target.label}.")
        elif status in {"succeeded", "failed"}:
            self.action_message = f"{target.label} is already {status} ({kind})"
            self.notify(f"{target.label} is already {status}.")
        else:
            observed = str(status or "unknown")
            self.action_message = (
                f"stop not confirmed for {target.label}; controller reports {observed}"
            )
            self.notify(
                f"Stop not confirmed for {target.label}; controller reports "
                f"{observed}. Refreshing controller state.",
                severity="warning",
            )
        self._start_probe()

    def _stop_failed(self, target: StopTarget, error: str) -> None:
        self.stop_in_progress = False
        self.action_message = f"stop failed for {target.label}: {error}"
        self.notify(
            f"Could not confirm stop for {target.label}: {error}. "
            "Refreshing controller state.",
            severity="error",
            timeout=10,
        )
        self._start_probe()

    def _tick(self) -> None:
        now = self._clock()
        age = _elapsed_seconds(self.last_refresh, now) if self.last_refresh else 0
        age_text = _duration(age) if self.snapshot is not None else "--"
        if self.probe_in_progress:
            state = "probing controller"
            probe_text = "probe in progress"
        elif self.last_error is not None:
            state = f"refresh failed: {self.last_error}"
            remaining = _countdown_seconds(self.next_probe_at, now)
            probe_text = f"next probe {_duration(remaining)}"
        else:
            state = (
                "controller online"
                if self.snapshot is not None
                else "controller unavailable"
            )
            remaining = _countdown_seconds(self.next_probe_at, now)
            probe_text = f"next probe {_duration(remaining)}"
        self.query_one("#topline", Static).update(
            Text(f"REMOTE RUNNER | {state} | snapshot age {age_text} | {probe_text}")
        )
        if self.snapshot is None:
            return
        servers = self.snapshot.get("servers", [])
        queue = self.snapshot.get("queue", [])
        summary = self.snapshot.get("summary")
        queue_count = len(queue)
        if isinstance(summary, dict):
            queue_summary = summary.get("queue")
            if isinstance(queue_summary, dict):
                active = queue_summary.get("active")
                if isinstance(active, int) and not isinstance(active, bool):
                    queue_count = active
        busy = sum(
            isinstance(server, dict) and server.get("state") == "busy"
            for server in servers
        )
        suffix = ""
        if age > self.interval * 2:
            suffix = " | STALE"
        if self.action_message is not None:
            suffix += f" | {self.action_message}"
        self.query_one("#statusline", Static).update(
            Text(
                f"{len(servers)} servers | {busy} busy | {queue_count} queued | "
                f"refresh interval {self.interval}s{suffix}"
            )
        )


def run_tui(args: argparse.Namespace) -> None:
    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    RemoteRunnerTui(
        args,
        interval=config.scheduling.probe_interval_seconds,
        stop_timeout=args.stop_timeout,
    ).run()
