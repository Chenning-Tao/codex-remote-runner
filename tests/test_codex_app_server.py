from __future__ import annotations

import os
import selectors
import time
from pathlib import Path
from typing import Any, Self

import pytest

from remote_runner._internal import codex_app_server


THREAD_ID = "019f93a3-2a16-7640-bd71-44aee4cc0fb2"
WAKE_ID = "rrw-" + "a" * 32


class FakeClient:
    def __init__(
        self,
        thread: dict[str, Any],
        *,
        threads: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.thread = thread
        self.threads = threads or {str(thread["id"]): thread}
        self.initialized = False
        self.reads: list[tuple[str, bool]] = []
        self.resume_ids: list[str] = []
        self.started: list[tuple[str, str, str]] = []
        self.waited: list[tuple[str, str]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        return None

    def initialize(self) -> None:
        self.initialized = True

    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = True,
    ) -> dict[str, Any]:
        self.reads.append((thread_id, include_turns))
        return self.threads[thread_id]

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        self.resume_ids.append(thread_id)
        return self.threads[thread_id]

    def start_turn(self, thread_id: str, wake_id: str, prompt: str) -> dict[str, Any]:
        self.started.append((thread_id, wake_id, prompt))
        return {"id": "turn-new", "status": "inProgress", "items": []}

    def wait_for_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.waited.append((thread_id, turn_id))
        return {"id": turn_id, "status": "completed", "items": []}


def user_turn(status: str = "completed") -> dict[str, Any]:
    return {
        "id": "turn-existing",
        "status": status,
        "items": [
            {
                "type": "userMessage",
                "id": "item-1",
                "clientId": WAKE_ID,
                "content": [],
            }
        ],
    }


def test_preflight_reads_the_exact_thread_without_starting_a_turn() -> None:
    client = FakeClient({"id": THREAD_ID, "turns": []})

    resolved = codex_app_server.preflight_thread(
        Path("/opt/codex"),
        THREAD_ID,
        client_factory=lambda _executable: client,
    )

    assert client.initialized is True
    assert resolved == THREAD_ID
    assert client.reads == [(THREAD_ID, False)]
    assert client.started == []


def test_preflight_and_delivery_route_subagents_to_the_root_thread() -> None:
    child_id = "019fa161-e274-7110-a7d7-3a4bad156043"
    child = {
        "id": child_id,
        "parentThreadId": THREAD_ID,
        "source": {"subAgent": {"thread_spawn": {"depth": 1}}},
        "turns": [],
    }
    parent = {"id": THREAD_ID, "source": {"cli": {}}, "turns": []}
    client = FakeClient(child, threads={child_id: child, THREAD_ID: parent})

    resolved = codex_app_server.preflight_thread(
        Path("/opt/codex"),
        child_id,
        client_factory=lambda _executable: client,
    )
    result = codex_app_server.commit_wakeup_turn(
        Path("/opt/codex"),
        child_id,
        WAKE_ID,
        "report this event",
        client_factory=lambda _executable: client,
    )

    assert resolved == THREAD_ID
    assert client.reads == [(child_id, False), (THREAD_ID, False)]
    assert client.resume_ids == [child_id, THREAD_ID]
    assert client.started == [(THREAD_ID, WAKE_ID, "report this event")]
    assert client.waited == [(THREAD_ID, "turn-new")]
    assert result["turn_status"] == "completed"


def test_history_commit_starts_one_turn_and_waits_for_completion() -> None:
    client = FakeClient({"id": THREAD_ID, "turns": []})

    result = codex_app_server.commit_wakeup_turn(
        Path("/opt/codex"),
        THREAD_ID,
        WAKE_ID,
        "report this event",
        client_factory=lambda _executable: client,
    )

    assert result == {
        "wake_id": WAKE_ID,
        "turn_id": "turn-new",
        "turn_status": "completed",
        "already_started": False,
        "visibility": "thread_history_only",
    }
    assert client.started == [(THREAD_ID, WAKE_ID, "report this event")]
    assert client.waited == [(THREAD_ID, "turn-new")]


def test_history_commit_uses_client_message_id_as_effectively_once_marker() -> None:
    client = FakeClient({"id": THREAD_ID, "turns": [user_turn()]})

    result = codex_app_server.commit_wakeup_turn(
        Path("/opt/codex"),
        THREAD_ID,
        WAKE_ID,
        "report this event",
        client_factory=lambda _executable: client,
    )

    assert result["already_started"] is True
    assert result["turn_id"] == "turn-existing"
    assert client.started == []
    assert client.waited == []


def test_ambiguous_retry_can_inspect_without_starting_again() -> None:
    client = FakeClient({"id": THREAD_ID, "turns": []})

    with pytest.raises(codex_app_server.WakeupTurnNotFound):
        codex_app_server.commit_wakeup_turn(
            Path("/opt/codex"),
            THREAD_ID,
            WAKE_ID,
            "report this event",
            start_if_missing=False,
            client_factory=lambda _executable: client,
        )

    assert client.started == []


def test_history_commit_rejoins_an_in_progress_matching_turn() -> None:
    client = FakeClient({"id": THREAD_ID, "turns": [user_turn("inProgress")]})

    result = codex_app_server.commit_wakeup_turn(
        Path("/opt/codex"),
        THREAD_ID,
        WAKE_ID,
        "report this event",
        client_factory=lambda _executable: client,
    )

    assert result["already_started"] is True
    assert result["turn_status"] == "completed"
    assert client.started == []
    assert client.waited == [(THREAD_ID, "turn-existing")]


def test_json_rpc_request_omits_jsonrpc_header_and_ignores_notifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(codex_app_server.AppServerClient)
    client._next_id = 7
    client._request_timeout = 15
    client._notifications = []
    sent: list[dict[str, Any]] = []
    messages = iter(
        [
            {"method": "thread/status/changed", "params": {}},
            {"id": 7, "result": {"ok": True}},
        ]
    )
    monkeypatch.setattr(client, "_send", sent.append)
    monkeypatch.setattr(client, "_read", lambda **_kwargs: next(messages))

    result = client._request("example/method", {"value": 1})

    assert result == {"ok": True}
    assert sent == [{"method": "example/method", "id": 7, "params": {"value": 1}}]
    assert "jsonrpc" not in sent[0]
    assert client._notifications == [
        {"method": "thread/status/changed", "params": {}}
    ]


def test_turn_start_sets_noninteractive_read_only_network_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(codex_app_server.AppServerClient)
    observed: list[tuple[str, dict[str, Any]]] = []

    def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        observed.append((method, params))
        return {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}

    monkeypatch.setattr(client, "_request", request)

    client.start_turn(THREAD_ID, WAKE_ID, "state changed")

    assert observed == [
        (
            "turn/start",
            {
                "threadId": THREAD_ID,
                "clientUserMessageId": WAKE_ID,
                "input": [{"type": "text", "text": "state changed"}],
                "approvalPolicy": "never",
                "sandboxPolicy": {
                    "type": "readOnly",
                    "networkAccess": True,
                },
            },
        )
    ]


def test_read_drains_coalesced_json_lines_without_waiting_for_the_fd_again() -> None:
    client = object.__new__(codex_app_server.AppServerClient)
    read_fd, write_fd = os.pipe()
    client._stdout = os.fdopen(read_fd, "rb", buffering=0)
    client._read_buffer = bytearray()
    client._selector = selectors.DefaultSelector()
    client._selector.register(client._stdout, selectors.EVENT_READ)
    messages = (
        b'{"id":1,"result":{"turn":{"id":"turn-1","status":"inProgress"}}}\n'
        b'{"method":"turn/completed","params":{"threadId":"thread-1",'
        b'"turn":{"id":"turn-1","status":"completed"}}}\n'
    )

    try:
        os.write(write_fd, messages)

        first = client._read(deadline=time.monotonic() + 1)
        second = client._read(deadline=time.monotonic() + 1)
    finally:
        os.close(write_fd)
        client._selector.close()
        client._stdout.close()

    assert first["id"] == 1
    assert second["method"] == "turn/completed"
