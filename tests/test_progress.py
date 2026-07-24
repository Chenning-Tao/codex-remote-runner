from __future__ import annotations

import json

import pytest

from remote_runner._internal.progress import (
    PROGRESS_PREFIX,
    decode_progress_event,
    parse_progress,
)


def event(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "scope": "c1_segment",
        "stage": "decode",
        "current": 8000,
        "total": 18000,
        "unit": "shots",
        "elapsed_seconds": 3600.0,
        "eta_seconds": 4500.0,
        "sequence": 12,
        "reported_at": "2026-07-21T12:00:00Z",
        "heartbeat": False,
        "detail": {"errors": 110, "segment_index": 2},
    }
    value.update(changes)
    return value


def line(**changes: object) -> str:
    return PROGRESS_PREFIX + json.dumps(event(**changes), separators=(",", ":"))


def test_decode_complete_structured_progress_event() -> None:
    assert decode_progress_event(line()[len(PROGRESS_PREFIX) :]) == {
        "kind": "structured_progress",
        "schema_version": 1,
        "scope": "c1_segment",
        "stage": "decode",
        "current": 8000,
        "total": 18000,
        "unit": "shots",
        "elapsed_seconds": 3600.0,
        "eta_seconds": 4500.0,
        "sequence": 12,
        "reported_at": "2026-07-21T12:00:00Z",
        "heartbeat": False,
        "percent": 100.0 * 8000 / 18000,
        "detail": {"errors": 110, "segment_index": 2},
    }


def test_nullable_counter_and_eta_are_valid_unknown_estimates() -> None:
    progress = parse_progress(
        line(current=None, total=None, eta_seconds=None, heartbeat=True)
    )

    assert progress is not None
    assert progress["kind"] == "structured_progress"
    assert progress["current"] is None
    assert progress["total"] is None
    assert progress["eta_seconds"] is None
    assert progress["heartbeat"] is True
    assert "percent" not in progress


def test_parser_uses_newest_structured_event() -> None:
    progress = parse_progress(f"{line(current=100)}\nnoise\n{line(current=200)}\n")

    assert progress is not None
    assert progress["current"] == 200


def test_malformed_newest_event_does_not_fall_back() -> None:
    progress = parse_progress(f"{line(current=100)}\n{PROGRESS_PREFIX}{{bad\n")

    assert progress is not None
    assert progress["kind"] == "invalid_progress"
    assert "invalid progress JSON" in progress["error"]


def test_legacy_progress_text_is_not_machine_progress() -> None:
    assert parse_progress("[PROGRESS] 75/100 (75.0%) | ETA=12s") is None
    assert parse_progress("progress shots=75/100 ETA=12s") is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_version": 2}, "schema_version=1"),
        ({"scope": "Bad Scope"}, "scope must match"),
        ({"current": True}, "current must be an integer"),
        ({"current": 19_000}, "current cannot exceed total"),
        ({"elapsed_seconds": float("nan")}, "elapsed_seconds must be"),
        ({"elapsed_seconds": 10**400}, "elapsed_seconds must be"),
        ({"eta_seconds": -1}, "eta_seconds must be"),
        ({"sequence": -1}, "sequence must be nonnegative"),
        ({"reported_at": "2026-07-21Z"}, "reported_at must be"),
        ({"reported_at": "2026-07-21T12:00:00+08:00"}, "reported_at must be"),
        ({"heartbeat": 1}, "heartbeat must be a boolean"),
        ({"detail": {"nested": {"bad": True}}}, "detail.nested must be a JSON scalar"),
        ({"extra": "field"}, "unknown fields: extra"),
    ],
)
def test_invalid_events_are_rejected(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        decode_progress_event(json.dumps(event(**changes)))
