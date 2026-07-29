from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import socket
import threading
import webbrowser
from typing import Any

from starlette.background import BackgroundTask
from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ._internal.config import load_managed_project_config
from ._internal.capacity_control import request_capacity_update
from ._internal.dashboard import query_dashboard
from ._internal.execution_registry import (
    resolve_project_config,
    validate_current_run_id,
)
from ._internal.experiment_client import (
    request_acceptance as request_experiment_acceptance,
)
from ._internal.experiment_client import request_query as request_experiment_query
from ._internal.experiment_contracts import MAX_CONTRACT_BYTES
from ._internal.queue_control import QueuePreparationError, request_queue_update
from ._internal.server_draining import request_server_drain_update
from ._internal.stopping import request_stop


WEB_SCHEMA_VERSION = 1
DEFAULT_WEB_PORT = 8765
STATIC_ROOT = Path(__file__).with_name("web_static")
SnapshotQuery = Callable[[argparse.Namespace], dict[str, Any]]
StopQuery = Callable[[argparse.Namespace], dict[str, Any]]
QueueUpdateQuery = Callable[[argparse.Namespace, str, dict[str, Any]], dict[str, Any]]
CapacityUpdateQuery = Callable[
    [argparse.Namespace, str, dict[str, Any]], dict[str, Any]
]
ServerDrainQuery = Callable[[argparse.Namespace, str, bool], dict[str, Any]]
BatchUpdateKey = tuple[
    tuple[tuple[str, int], ...],
    str | None,
    str | None,
    tuple[str, ...] | None,
]
BatchUpdateResult = tuple[list[str], list[dict[str, str]]]
BatchUpdateOperation = Callable[[], Awaitable[BatchUpdateResult]]
ExperimentQuery = Callable[[argparse.Namespace, dict[str, Any]], dict[str, Any]]
ExperimentAcceptance = Callable[[argparse.Namespace, dict[str, Any]], dict[str, Any]]


def _concise_controller_error(exc: Exception) -> str:
    detail = str(exc).strip()
    marker = ": error: "
    if marker in detail:
        return detail.rsplit(marker, 1)[1].strip()
    return detail


def utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


@dataclass(frozen=True)
class DashboardState:
    sequence: int
    status: str
    snapshot: dict[str, Any] | None
    error: str | None
    refreshed_at: datetime | None
    next_probe_at: datetime | None


class DashboardProbe:
    def __init__(
        self,
        args: argparse.Namespace,
        *,
        project_id: str,
        interval: int,
        query: SnapshotQuery = query_dashboard,
    ) -> None:
        self.args = args
        self.project_id = project_id
        self.interval = interval
        self.query = query
        self._condition = asyncio.Condition()
        self._probe_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._state = DashboardState(0, "connecting", None, None, None, None)

    def document(self) -> dict[str, Any]:
        state = self._state
        return {
            "schema_version": WEB_SCHEMA_VERSION,
            "project_id": self.project_id,
            "sequence": state.sequence,
            "status": state.status,
            "snapshot": state.snapshot,
            "error": state.error,
            "refreshed_at": _timestamp(state.refreshed_at),
            "next_probe_at": _timestamp(state.next_probe_at),
            "probe_interval_seconds": self.interval,
        }

    async def _publish(
        self,
        *,
        status: str,
        snapshot: dict[str, Any] | None,
        error: str | None,
        refreshed_at: datetime | None,
        next_probe_at: datetime | None,
    ) -> None:
        async with self._condition:
            self._state = DashboardState(
                self._state.sequence + 1,
                status,
                snapshot,
                error,
                refreshed_at,
                next_probe_at,
            )
            self._condition.notify_all()

    async def probe_once(self) -> None:
        async with self._probe_lock:
            previous = self._state
            await self._publish(
                status="probing",
                snapshot=previous.snapshot,
                error=None,
                refreshed_at=previous.refreshed_at,
                next_probe_at=None,
            )
            try:
                snapshot = await asyncio.to_thread(self.query, self.args)
            except (OSError, RuntimeError, ValueError) as exc:
                completed_at = utc_now()
                await self._publish(
                    status="error",
                    snapshot=previous.snapshot,
                    error=str(exc),
                    refreshed_at=previous.refreshed_at,
                    next_probe_at=completed_at + timedelta(seconds=self.interval),
                )
                return
            completed_at = utc_now()
            await self._publish(
                status="online",
                snapshot=snapshot,
                error=None,
                refreshed_at=completed_at,
                next_probe_at=completed_at + timedelta(seconds=self.interval),
            )

    async def _run(self) -> None:
        while True:
            await self.probe_once()
            await asyncio.sleep(self.interval)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="dashboard-probe")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def wait_for_update(
        self, sequence: int, *, timeout: float
    ) -> dict[str, Any] | None:
        async with self._condition:
            if self._state.sequence == sequence:
                try:
                    await asyncio.wait_for(
                        self._condition.wait_for(
                            lambda: self._state.sequence != sequence
                        ),
                        timeout=timeout,
                    )
                except TimeoutError:
                    return None
            return self.document()


class InFlightBatchUpdates:
    def __init__(self) -> None:
        self._tasks: dict[BatchUpdateKey, asyncio.Task[BatchUpdateResult]] = {}

    async def run(
        self,
        key: BatchUpdateKey,
        operation: BatchUpdateOperation,
    ) -> BatchUpdateResult:
        task = self._tasks.get(key)
        if task is None:

            async def execute() -> BatchUpdateResult:
                return await operation()

            task = asyncio.create_task(execute(), name="queue-batch-update")
            self._tasks[key] = task

            def discard(completed: asyncio.Task[BatchUpdateResult]) -> None:
                if self._tasks.get(key) is completed:
                    del self._tasks[key]

            task.add_done_callback(discard)
        return await asyncio.shield(task)


class SecurityHeadersMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        async def send_with_headers(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    (
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; connect-src 'self'; "
                            b"img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                            b"script-src 'self'; frame-ancestors 'none'",
                        ),
                    )
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(
    probe: DashboardProbe,
    *,
    static_root: Path = STATIC_ROOT,
    manage_probe: bool = True,
    stop_query: StopQuery = request_stop,
    queue_update_query: QueueUpdateQuery = request_queue_update,
    capacity_update_query: CapacityUpdateQuery = request_capacity_update,
    server_drain_query: ServerDrainQuery = request_server_drain_update,
    experiment_query: ExperimentQuery = request_experiment_query,
    experiment_acceptance: ExperimentAcceptance = request_experiment_acceptance,
) -> Starlette:
    if not (static_root / "index.html").is_file():
        raise RuntimeError(
            f"web assets are unavailable at {static_root}; rebuild the web frontend"
        )
    in_flight_batch_updates = InFlightBatchUpdates()

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        if manage_probe:
            await probe.start()
        try:
            yield
        finally:
            if manage_probe:
                await probe.stop()

    async def snapshot_endpoint(_request: Request) -> Response:
        return JSONResponse(
            probe.document(),
            headers={"Cache-Control": "no-store"},
        )

    async def experiment_query_endpoint(request: Request) -> Response:
        content_type = (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        if content_type != "application/json":
            return JSONResponse(
                {"error": "experiment query must use application/json"},
                status_code=415,
            )
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_CONTRACT_BYTES:
                    return JSONResponse(
                        {"error": "experiment_query_too_large"}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"error": "invalid content-length"}, status_code=400
                )
        try:
            body = await request.body()
            if len(body) > MAX_CONTRACT_BYTES:
                return JSONResponse(
                    {"error": "experiment_query_too_large"}, status_code=413
                )
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": "experiment query must be a JSON object"}, status_code=400
            )
        query_args = argparse.Namespace(**vars(probe.args))
        try:
            result = await asyncio.to_thread(
                experiment_query,
                query_args,
                payload,
            )
        except FileNotFoundError as exc:
            return JSONResponse(
                {"error": "experiment_not_found", "detail": str(exc)},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as exc:
            return JSONResponse(
                {"error": "invalid_experiment_query", "detail": str(exc)},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        except (OSError, RuntimeError) as exc:
            detail = _concise_controller_error(exc)
            if "does not exist" in detail:
                status_code = 404
                error = "experiment_not_found"
            elif "cursor expired" in detail or "revision conflict" in detail:
                status_code = 409
                error = "experiment_query_conflict"
            else:
                status_code = 502
                error = "experiment_query_failed"
            return JSONResponse(
                {"error": error, "detail": detail},
                status_code=status_code,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    async def experiment_acceptance_endpoint(request: Request) -> Response:
        if request.headers.get("x-remote-runner-action") != "decide-experiment-result":
            return JSONResponse(
                {"error": "missing experiment decision action header"},
                status_code=403,
            )
        content_type = (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        if content_type != "application/json":
            return JSONResponse(
                {"error": "experiment decision must use application/json"},
                status_code=415,
            )
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_CONTRACT_BYTES:
                    return JSONResponse(
                        {"error": "experiment_decision_too_large"},
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse(
                    {"error": "invalid content-length"},
                    status_code=400,
                )
        try:
            body = await request.body()
            if len(body) > MAX_CONTRACT_BYTES:
                return JSONResponse(
                    {"error": "experiment_decision_too_large"},
                    status_code=413,
                )
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": "experiment decision must be a JSON object"},
                status_code=400,
            )
        decision_args = argparse.Namespace(**vars(probe.args))
        try:
            result = await asyncio.to_thread(
                experiment_acceptance,
                decision_args,
                payload,
            )
        except FileNotFoundError as exc:
            return JSONResponse(
                {"error": "experiment_not_found", "detail": str(exc)},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as exc:
            return JSONResponse(
                {"error": "invalid_experiment_decision", "detail": str(exc)},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        except (OSError, RuntimeError) as exc:
            detail = _concise_controller_error(exc)
            status_code = 409 if "conflict" in detail else 502
            error = (
                "experiment_decision_conflict"
                if status_code == 409
                else "experiment_decision_failed"
            )
            return JSONResponse(
                {"error": error, "detail": detail},
                status_code=status_code,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    async def stop_endpoint(request: Request) -> Response:
        if request.headers.get("x-remote-runner-action") != "stop":
            return JSONResponse(
                {"error": "missing stop action header"}, status_code=403
            )
        content_type = (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        if content_type != "application/json":
            return JSONResponse(
                {"error": "stop request must use application/json"}, status_code=415
            )
        run_id = request.path_params["run_id"]
        try:
            validate_current_run_id(run_id)
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"run_id", "confirm"}
            or payload.get("run_id") != run_id
            or payload.get("confirm") is not True
        ):
            return JSONResponse(
                {"error": "stop request confirmation is invalid"}, status_code=400
            )
        stop_args = argparse.Namespace(**vars(probe.args))
        stop_args.run_id = run_id
        stop_args.timeout = getattr(probe.args, "stop_timeout", 10)
        try:
            result = await asyncio.to_thread(stop_query, stop_args)
        except (OSError, RuntimeError, ValueError) as exc:
            detail = _concise_controller_error(exc)
            if detail.startswith("controller run does not exist:"):
                await probe.probe_once()
                return JSONResponse(
                    {"error": "run_not_found"},
                    status_code=404,
                    headers={"Cache-Control": "no-store"},
                )
            if "run is currently being dispatched" in detail:
                return JSONResponse({"error": "run_dispatching"}, status_code=409)
            return JSONResponse(
                {"error": "stop_failed", "detail": detail}, status_code=409
            )
        await probe.probe_once()
        return JSONResponse(
            {"status": "stopped", "run_id": run_id, "result": result},
            headers={"Cache-Control": "no-store"},
        )

    async def queue_update_endpoint(request: Request) -> Response:
        if request.headers.get("x-remote-runner-action") != "update-queue":
            return JSONResponse(
                {"error": "missing queue update action header"}, status_code=403
            )
        content_type = (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        if content_type != "application/json":
            return JSONResponse(
                {"error": "queue update request must use application/json"},
                status_code=415,
            )
        run_id = request.path_params["run_id"]
        try:
            validate_current_run_id(run_id)
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        allowed = {
            "run_id",
            "expected_revision",
            "queue_priority",
            "workload_class",
            "eligible_servers",
            "move",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) - allowed
            or payload.get("run_id") != run_id
            or isinstance(payload.get("expected_revision"), bool)
            or not isinstance(payload.get("expected_revision"), int)
            or not any(
                field in payload
                for field in (
                    "queue_priority",
                    "workload_class",
                    "eligible_servers",
                    "move",
                )
            )
        ):
            return JSONResponse(
                {"error": "queue update request is invalid"}, status_code=400
            )
        controller_payload = dict(payload)
        controller_payload.pop("run_id")
        update_args = argparse.Namespace(**vars(probe.args))
        try:
            result = await asyncio.to_thread(
                queue_update_query,
                update_args,
                run_id,
                controller_payload,
            )
        except QueuePreparationError as exc:
            await probe.probe_once()
            return JSONResponse(
                {"error": "queue_preparation_failed", "detail": str(exc)},
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )
        except (OSError, RuntimeError, ValueError) as exc:
            detail = _concise_controller_error(exc)
            if detail.startswith("queued run does not exist:"):
                await probe.probe_once()
                return JSONResponse(
                    {"error": "queue_not_found"},
                    status_code=404,
                    headers={"Cache-Control": "no-store"},
                )
            if detail == "queued state revision conflict" or detail.endswith(
                "placement update in progress"
            ):
                await probe.probe_once()
                return JSONResponse(
                    {"error": "queue_conflict"},
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )
            if detail.endswith(", not editable"):
                await probe.probe_once()
                return JSONResponse(
                    {"error": "queue_not_editable"},
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(
                {"error": "invalid_queue_update", "detail": detail},
                status_code=400,
            )
        return JSONResponse(
            {"status": "updated", "run_id": run_id, "result": result},
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(probe.probe_once),
        )

    async def queue_batch_update_endpoint(request: Request) -> Response:
        if request.headers.get("x-remote-runner-action") != "update-queue-batch":
            return JSONResponse(
                {"error": "missing batch queue update action header"},
                status_code=403,
            )
        content_type = (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        if content_type != "application/json":
            return JSONResponse(
                {"error": "batch queue update request must use application/json"},
                status_code=415,
            )
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        updates = payload.get("updates") if isinstance(payload, dict) else None
        queue_priority = (
            payload.get("queue_priority") if isinstance(payload, dict) else None
        )
        workload_class = (
            payload.get("workload_class") if isinstance(payload, dict) else None
        )
        eligible_servers = (
            payload.get("eligible_servers") if isinstance(payload, dict) else None
        )
        allowed_fields = {
            "updates",
            "queue_priority",
            "workload_class",
            "eligible_servers",
        }
        if (
            not isinstance(payload, dict)
            or not set(payload) <= allowed_fields
            or "updates" not in payload
            or len(set(payload) - {"updates"}) == 0
            or not isinstance(updates, list)
            or not 1 <= len(updates) <= 100
            or (
                "queue_priority" in payload
                and (
                    not isinstance(queue_priority, str)
                    or queue_priority not in {"urgent", "normal"}
                )
            )
            or (
                "workload_class" in payload
                and (
                    not isinstance(workload_class, str)
                    or workload_class not in {"standard", "test"}
                )
            )
            or (
                "eligible_servers" in payload
                and (
                    not isinstance(eligible_servers, list)
                    or not eligible_servers
                    or len(eligible_servers) > 100
                    or any(
                        not isinstance(server, str) or not server
                        for server in eligible_servers
                    )
                    or len(set(eligible_servers)) != len(eligible_servers)
                )
            )
        ):
            return JSONResponse(
                {"error": "batch queue update request is invalid"}, status_code=400
            )

        normalized_updates: list[tuple[str, int]] = []
        try:
            for update in updates:
                if (
                    not isinstance(update, dict)
                    or set(update) != {"run_id", "expected_revision"}
                    or not isinstance(update.get("run_id"), str)
                    or isinstance(update.get("expected_revision"), bool)
                    or not isinstance(update.get("expected_revision"), int)
                ):
                    raise ValueError("batch queue update item is invalid")
                normalized_updates.append(
                    (
                        validate_current_run_id(update["run_id"]),
                        update["expected_revision"],
                    )
                )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        run_ids = [run_id for run_id, _revision in normalized_updates]
        if len(set(run_ids)) != len(run_ids):
            return JSONResponse(
                {"error": "batch queue update contains duplicate runs"},
                status_code=400,
            )

        snapshot = probe.document().get("snapshot")
        snapshot_entries = (
            snapshot.get("queue", []) if isinstance(snapshot, dict) else []
        )
        initial_jobs = {
            entry["job"]["run_id"]: entry["job"]
            for entry in snapshot_entries
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("job"), dict)
                and isinstance(entry["job"].get("run_id"), str)
            )
        }
        ordering_change_expected = {
            run_id: (
                run_id not in initial_jobs
                or (
                    (
                        queue_priority is not None
                        and initial_jobs[run_id].get("queue_priority", "normal")
                        != queue_priority
                    )
                    or (
                        workload_class is not None
                        and initial_jobs[run_id].get("workload_class", "standard")
                        != workload_class
                    )
                )
            )
            for run_id in run_ids
        }
        controller_changes = {
            key: payload[key]
            for key in ("queue_priority", "workload_class", "eligible_servers")
            if key in payload
        }

        async def apply_updates() -> BatchUpdateResult:
            update_args = argparse.Namespace(**vars(probe.args))
            succeeded: list[str] = []
            failed: list[dict[str, str]] = []
            ordering_revision_offset = 0
            for run_id, expected_revision in normalized_updates:
                try:
                    await asyncio.to_thread(
                        queue_update_query,
                        update_args,
                        run_id,
                        {
                            "expected_revision": (
                                expected_revision + ordering_revision_offset
                            ),
                            **controller_changes,
                        },
                    )
                    succeeded.append(run_id)
                    if ordering_change_expected[run_id]:
                        ordering_revision_offset += 1
                except QueuePreparationError as exc:
                    failed.append(
                        {
                            "run_id": run_id,
                            "error": "queue_preparation_failed",
                            "detail": str(exc),
                        }
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    detail = _concise_controller_error(exc)
                    if detail.startswith("queued run does not exist:"):
                        code = "queue_not_found"
                    elif detail == "queued state revision conflict" or detail.endswith(
                        "placement update in progress"
                    ):
                        code = "queue_conflict"
                    elif detail.endswith(", not editable"):
                        code = "queue_not_editable"
                    else:
                        code = "invalid_queue_update"
                    failed.append({"run_id": run_id, "error": code, "detail": detail})

            await probe.probe_once()
            return succeeded, failed

        batch_key = (
            tuple(normalized_updates),
            queue_priority,
            workload_class,
            tuple(eligible_servers) if isinstance(eligible_servers, list) else None,
        )
        succeeded, failed = await in_flight_batch_updates.run(batch_key, apply_updates)
        status = "updated" if not failed else "partial" if succeeded else "failed"
        return JSONResponse(
            {"status": status, "succeeded": succeeded, "failed": failed},
            status_code=200 if not failed else 207,
            headers={"Cache-Control": "no-store"},
        )

    async def capacity_update_endpoint(request: Request) -> Response:
        if request.headers.get("x-remote-runner-action") != "update-capacity":
            return JSONResponse(
                {"error": "missing capacity update action header"}, status_code=403
            )
        content_type = (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        if content_type != "application/json":
            return JSONResponse(
                {"error": "capacity update request must use application/json"},
                status_code=415,
            )
        server = request.path_params["server"]
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"server", "expected_revision", "standard_slots", "test_slots"}
            or payload.get("server") != server
            or isinstance(payload.get("expected_revision"), bool)
            or not isinstance(payload.get("expected_revision"), int)
            or any(
                isinstance(payload.get(field), bool)
                or not isinstance(payload.get(field), int)
                or not 0 <= payload[field] <= 1024
                for field in ("standard_slots", "test_slots")
            )
        ):
            return JSONResponse(
                {"error": "capacity update request is invalid"}, status_code=400
            )
        snapshot = probe.document().get("snapshot")
        known_servers = (
            {
                item.get("name")
                for item in snapshot.get("servers", [])
                if isinstance(snapshot, dict) and isinstance(item, dict)
            }
            if isinstance(snapshot, dict)
            else set()
        )
        if server not in known_servers:
            return JSONResponse({"error": "capacity_not_found"}, status_code=404)
        controller_payload = dict(payload)
        controller_payload.pop("server")
        update_args = argparse.Namespace(**vars(probe.args))
        try:
            result = await asyncio.to_thread(
                capacity_update_query,
                update_args,
                server,
                controller_payload,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            detail = _concise_controller_error(exc)
            if detail == "server capacity revision conflict":
                await probe.probe_once()
                return JSONResponse(
                    {"error": "capacity_conflict"},
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )
            if detail.startswith("server capacity does not exist:"):
                await probe.probe_once()
                return JSONResponse(
                    {"error": "capacity_not_found"},
                    status_code=404,
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(
                {"error": "invalid_capacity_update", "detail": detail},
                status_code=400,
            )
        await probe.probe_once()
        return JSONResponse(
            {"status": "updated", "server": server, "result": result},
            headers={"Cache-Control": "no-store"},
        )

    async def server_drain_endpoint(request: Request) -> Response:
        operation = request.path_params["operation"]
        drained = operation == "drain"
        expected_action = "drain-server" if drained else "resume-server"
        if operation not in {"drain", "resume"}:
            return JSONResponse(
                {"error": "invalid server drain operation"}, status_code=404
            )
        if request.headers.get("x-remote-runner-action") != expected_action:
            return JSONResponse(
                {"error": f"missing {expected_action} action header"},
                status_code=403,
            )
        content_type = (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        if content_type != "application/json":
            return JSONResponse(
                {"error": "server drain request must use application/json"},
                status_code=415,
            )
        server = request.path_params["server"]
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"server", "confirm"}
            or payload.get("server") != server
            or payload.get("confirm") is not True
        ):
            return JSONResponse(
                {"error": "server drain confirmation is invalid"}, status_code=400
            )
        snapshot = probe.document().get("snapshot")
        known_servers = (
            {
                item.get("name")
                for item in snapshot.get("servers", [])
                if isinstance(snapshot, dict) and isinstance(item, dict)
            }
            if isinstance(snapshot, dict)
            else set()
        )
        if server not in known_servers:
            return JSONResponse({"error": "server_not_found"}, status_code=404)
        update_args = argparse.Namespace(**vars(probe.args))
        try:
            result = await asyncio.to_thread(
                server_drain_query,
                update_args,
                server,
                drained,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            detail = _concise_controller_error(exc)
            if "not configured for this project" in detail:
                await probe.probe_once()
                return JSONResponse(
                    {"error": "server_not_found"},
                    status_code=404,
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(
                {"error": "server_drain_failed", "detail": detail},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        await probe.probe_once()
        return JSONResponse(
            {
                "status": "drained" if drained else "resumed",
                "server": server,
                "result": result,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def events_endpoint(request: Request) -> Response:
        async def stream() -> AsyncIterator[str]:
            sequence = -1
            while not await request.is_disconnected():
                document = await probe.wait_for_update(sequence, timeout=15)
                if document is None:
                    yield ": keepalive\n\n"
                    continue
                sequence = int(document["sequence"])
                yield "event: snapshot\n"
                yield f"data: {json.dumps(document, separators=(',', ':'))}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    app = Starlette(
        routes=[
            Route("/api/snapshot", snapshot_endpoint),
            Route("/api/events", events_endpoint),
            Route(
                "/api/experiments/query",
                experiment_query_endpoint,
                methods=["POST"],
            ),
            Route(
                "/api/experiments/acceptances",
                experiment_acceptance_endpoint,
                methods=["POST"],
            ),
            Route("/api/runs/{run_id:str}/stop", stop_endpoint, methods=["POST"]),
            Route(
                "/api/queue/{run_id:str}",
                queue_update_endpoint,
                methods=["PATCH"],
            ),
            Route(
                "/api/queue-batch",
                queue_batch_update_endpoint,
                methods=["PATCH"],
            ),
            Route(
                "/api/servers/{server:str}/capacity",
                capacity_update_endpoint,
                methods=["PATCH"],
            ),
            Route(
                "/api/servers/{server:str}/{operation:str}",
                server_drain_endpoint,
                methods=["POST"],
            ),
            Mount("/", StaticFiles(directory=static_root, html=True), name="web"),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _open_browser_when_ready(port: int) -> None:
    url = f"http://127.0.0.1:{port}/"

    def open_browser() -> None:
        for _attempt in range(100):
            with socket.socket() as client:
                client.settimeout(0.1)
                if client.connect_ex(("127.0.0.1", port)) == 0:
                    webbrowser.open(url)
                    return
            threading.Event().wait(0.05)

    threading.Thread(target=open_browser, daemon=True).start()


def run_web(args: argparse.Namespace) -> None:
    import uvicorn

    config_path = resolve_project_config(args.project_config)
    config = load_managed_project_config(config_path)
    probe = DashboardProbe(
        args,
        project_id=config.project_id,
        interval=config.scheduling.probe_interval_seconds,
    )
    app = create_app(probe)
    if not args.no_open:
        _open_browser_when_ready(args.port)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        log_level="info",
    )
