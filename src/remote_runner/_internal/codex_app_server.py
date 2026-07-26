from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Protocol, Self

from .. import __version__


REQUEST_TIMEOUT_SECONDS = 15
TURN_OBSERVE_SECONDS = 300
TERMINAL_TURN_STATUSES = {"completed", "interrupted", "failed"}


class AppServerError(RuntimeError):
    pass


class WakeupTurnNotFound(AppServerError):
    pass


class AppServerSession(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None: ...

    def initialize(self) -> None: ...

    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = True,
    ) -> dict[str, Any]: ...

    def resume_thread(self, thread_id: str) -> dict[str, Any]: ...

    def start_turn(self, thread_id: str, wake_id: str, prompt: str) -> dict[str, Any]: ...

    def wait_for_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]: ...


AppServerFactory = Callable[[Path], AppServerSession]


def resolve_codex_executable(value: Path | None) -> Path:
    if value is None:
        discovered = shutil.which("codex")
        if discovered is None:
            raise FileNotFoundError("could not find the Codex CLI executable")
        candidate = Path(discovered)
    else:
        candidate = value.expanduser()
        if not candidate.is_absolute():
            raise ValueError("--codex-executable must be an absolute path")
    resolved = candidate.resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise FileNotFoundError(f"Codex CLI executable is unavailable: {resolved}")
    return resolved


def validate_thread_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError("Codex thread id must be a non-empty single-line string")
    return value


class AppServerClient:
    def __init__(
        self,
        executable: Path,
        *,
        request_timeout: int = REQUEST_TIMEOUT_SECONDS,
        turn_timeout: int = TURN_OBSERVE_SECONDS,
    ) -> None:
        if request_timeout <= 0 or turn_timeout <= 0:
            raise ValueError("App Server timeouts must be positive")
        self._request_timeout = request_timeout
        self._turn_timeout = turn_timeout
        self._next_id = 1
        self._notifications: list[dict[str, Any]] = []
        try:
            self._process = subprocess.Popen(
                [str(executable), "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                close_fds=True,
            )
        except OSError as exc:
            raise AppServerError(f"failed to start Codex App Server: {exc}") from exc
        if self._process.stdin is None or self._process.stdout is None:
            self.close()
            raise AppServerError("Codex App Server did not expose stdio pipes")
        self._stdin = self._process.stdin
        self._stdout = self._process.stdout
        self._read_buffer = bytearray()
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._stdout, selectors.EVENT_READ)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self.close()

    def close(self) -> None:
        selector = getattr(self, "_selector", None)
        if selector is not None:
            selector.close()
        process = getattr(self, "_process", None)
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def _send(self, message: dict[str, Any]) -> None:
        try:
            self._stdin.write(
                (
                    json.dumps(message, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
            )
            self._stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerError("Codex App Server connection closed while writing") from exc

    def _read(self, *, deadline: float) -> dict[str, Any]:
        while b"\n" not in self._read_buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._selector.select(remaining):
                raise AppServerError("Codex App Server response timed out")
            try:
                chunk = os.read(self._stdout.fileno(), 65536)
            except OSError as exc:
                raise AppServerError(
                    "Codex App Server connection closed while reading"
                ) from exc
            if not chunk:
                returncode = self._process.poll()
                detail = (
                    "closed its output"
                    if returncode is None
                    else f"exited with {returncode}"
                )
                raise AppServerError(f"Codex App Server {detail}")
            self._read_buffer.extend(chunk)
        raw_line, _, remainder = self._read_buffer.partition(b"\n")
        self._read_buffer = bytearray(remainder)
        try:
            message = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppServerError("Codex App Server returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise AppServerError("Codex App Server returned a non-object message")
        return message

    def _reject_server_request(self, message: dict[str, Any]) -> None:
        self._send(
            {
                "id": message["id"],
                "error": {
                    "code": -32601,
                    "message": "remote-runner wakeup cannot service interactive requests",
                },
            }
        )

    def _request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self._request_timeout
        while True:
            message = self._read(deadline=deadline)
            if "method" in message and "id" in message:
                self._reject_server_request(message)
                continue
            if "method" in message:
                self._notifications.append(message)
                continue
            if message.get("id") != request_id:
                continue
            error = message.get("error")
            if isinstance(error, dict):
                detail = error.get("message")
                if not isinstance(detail, str) or not detail:
                    detail = "unknown App Server error"
                raise AppServerError(f"Codex App Server {method} failed: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerError(
                    f"Codex App Server {method} returned an invalid result"
                )
            return result

    def initialize(self) -> None:
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_remote_runner",
                    "title": "Codex Remote Runner",
                    "version": __version__,
                }
            },
        )
        self._send({"method": "initialized", "params": {}})

    @staticmethod
    def _thread(result: dict[str, Any], expected_thread_id: str) -> dict[str, Any]:
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != expected_thread_id:
            raise AppServerError("Codex App Server thread identity mismatch")
        turns = thread.get("turns")
        if turns is not None and not isinstance(turns, list):
            raise AppServerError("Codex App Server returned invalid thread turns")
        return thread

    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = True,
    ) -> dict[str, Any]:
        validated = validate_thread_id(thread_id)
        return self._thread(
            self._request(
                "thread/read",
                {"threadId": validated, "includeTurns": include_turns},
            ),
            validated,
        )

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        validated = validate_thread_id(thread_id)
        return self._thread(
            self._request("thread/resume", {"threadId": validated}),
            validated,
        )

    @staticmethod
    def _turn(result: dict[str, Any]) -> dict[str, Any]:
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise AppServerError("Codex App Server returned an invalid turn")
        if turn.get("status") not in {
            "completed",
            "interrupted",
            "failed",
            "inProgress",
        }:
            raise AppServerError("Codex App Server returned an invalid turn status")
        return turn

    def start_turn(self, thread_id: str, wake_id: str, prompt: str) -> dict[str, Any]:
        return self._turn(
            self._request(
                "turn/start",
                {
                    "threadId": validate_thread_id(thread_id),
                    "clientUserMessageId": wake_id,
                    "input": [{"type": "text", "text": prompt}],
                    "approvalPolicy": "never",
                    "sandboxPolicy": {
                        "type": "readOnly",
                        "networkAccess": True,
                    },
                },
            )
        )

    def wait_for_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self._turn_timeout
        while True:
            message = (
                self._notifications.pop(0)
                if self._notifications
                else self._read(deadline=deadline)
            )
            if "method" in message and "id" in message:
                self._reject_server_request(message)
                continue
            if message.get("method") != "turn/completed":
                continue
            params = message.get("params")
            if not isinstance(params, dict) or params.get("threadId") != thread_id:
                continue
            turn = self._turn(params)
            if turn["id"] == turn_id:
                return turn


def find_wakeup_turn(thread: dict[str, Any], wake_id: str) -> dict[str, Any] | None:
    turns = thread.get("turns", [])
    if not isinstance(turns, list):
        raise AppServerError("Codex App Server returned invalid thread turns")
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items", [])
        if not isinstance(items, list):
            continue
        if any(
            isinstance(item, dict)
            and item.get("type") == "userMessage"
            and item.get("clientId") == wake_id
            for item in items
        ):
            if turn.get("status") not in TERMINAL_TURN_STATUSES | {"inProgress"}:
                raise AppServerError("matching Codex wakeup turn has an invalid status")
            if not isinstance(turn.get("id"), str):
                raise AppServerError("matching Codex wakeup turn has no id")
            return turn
    return None


def preflight_thread(
    executable: Path,
    thread_id: str,
    *,
    client_factory: AppServerFactory = AppServerClient,
) -> None:
    with client_factory(executable) as client:
        client.initialize()
        client.read_thread(validate_thread_id(thread_id), include_turns=False)


def commit_wakeup_turn(
    executable: Path,
    thread_id: str,
    wake_id: str,
    prompt: str,
    *,
    start_if_missing: bool = True,
    client_factory: AppServerFactory = AppServerClient,
) -> dict[str, Any]:
    with client_factory(executable) as client:
        client.initialize()
        thread = client.resume_thread(validate_thread_id(thread_id))
        existing = find_wakeup_turn(thread, wake_id)
        if existing is None:
            if not start_if_missing:
                raise WakeupTurnNotFound(
                    f"Codex thread does not yet contain wakeup {wake_id}"
                )
            turn = client.start_turn(thread_id, wake_id, prompt)
            duplicate = False
        else:
            turn = existing
            duplicate = True
        if turn["status"] == "inProgress":
            turn = client.wait_for_turn(thread_id, str(turn["id"]))
        return {
            "wake_id": wake_id,
            "turn_id": turn["id"],
            "turn_status": turn["status"],
            "already_started": duplicate,
            "visibility": "thread_history_only",
        }
