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

from starlette.applications import Starlette
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from ._internal.config import load_managed_project_config
from ._internal.dashboard import query_dashboard
from ._internal.execution_registry import resolve_project_config


WEB_SCHEMA_VERSION = 1
DEFAULT_WEB_PORT = 8765
STATIC_ROOT = Path(__file__).with_name("web_static")
SnapshotQuery = Callable[[argparse.Namespace], dict[str, Any]]


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
) -> Starlette:
    if not (static_root / "index.html").is_file():
        raise RuntimeError(
            f"web assets are unavailable at {static_root}; rebuild the web frontend"
        )

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
