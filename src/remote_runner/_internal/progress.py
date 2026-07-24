from __future__ import annotations

from datetime import datetime
import json
import math
import re
from typing import Any


PROGRESS_PREFIX = "[REMOTE_RUNNER_PROGRESS] "
PROGRESS_SCHEMA_VERSION = 1

_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DETAIL_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RFC3339_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "stage",
        "current",
        "total",
        "unit",
        "elapsed_seconds",
        "eta_seconds",
        "sequence",
        "reported_at",
        "heartbeat",
    }
)
_OPTIONAL_FIELDS = frozenset({"detail"})
_MAX_DETAIL_FIELDS = 32
_MAX_DETAIL_STRING_LENGTH = 256
_MAX_ERROR_LENGTH = 240


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite nonnegative number")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite nonnegative number") from exc
    if not math.isfinite(normalized) or normalized < 0.0:
        raise ValueError(f"{field} must be a finite nonnegative number")
    return normalized


def _counter(value: object, *, field: str, positive: bool = False) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer or null")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"{field} must be {qualifier}")
    return value


def _token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must match {_TOKEN_RE.pattern}")
    return value


def _reported_at(value: object) -> str:
    if not isinstance(value, str) or _RFC3339_UTC_RE.fullmatch(value) is None:
        raise ValueError("reported_at must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError as exc:
        raise ValueError("reported_at must be an RFC 3339 UTC timestamp") from exc
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("reported_at must be an RFC 3339 UTC timestamp")
    return value


def _detail(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("detail must be an object")
    if len(value) > _MAX_DETAIL_FIELDS:
        raise ValueError(f"detail may contain at most {_MAX_DETAIL_FIELDS} fields")
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _DETAIL_KEY_RE.fullmatch(key) is None:
            raise ValueError(f"detail key must match {_DETAIL_KEY_RE.pattern}")
        if item is None or isinstance(item, bool) or isinstance(item, int):
            normalized[key] = item
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"detail.{key} must be finite")
            normalized[key] = item
        elif isinstance(item, str):
            if len(item) > _MAX_DETAIL_STRING_LENGTH:
                raise ValueError(
                    f"detail.{key} exceeds {_MAX_DETAIL_STRING_LENGTH} characters"
                )
            normalized[key] = item
        else:
            raise ValueError(f"detail.{key} must be a JSON scalar")
    return normalized


def decode_progress_event(encoded: str) -> dict[str, Any]:
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid progress JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("progress event must be a JSON object")

    fields = frozenset(payload)
    missing = sorted(_REQUIRED_FIELDS - fields)
    unknown = sorted(fields - _REQUIRED_FIELDS - _OPTIONAL_FIELDS)
    if missing:
        raise ValueError(f"progress event is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"progress event has unknown fields: {', '.join(unknown)}")

    schema_version = payload["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != PROGRESS_SCHEMA_VERSION
    ):
        raise ValueError(
            f"progress event requires schema_version={PROGRESS_SCHEMA_VERSION}"
        )

    current = _counter(payload["current"], field="current")
    total = _counter(payload["total"], field="total", positive=True)
    if total is not None and current is None:
        raise ValueError("current cannot be null when total is set")
    if current is not None and total is not None and current > total:
        raise ValueError("current cannot exceed total")

    sequence = _counter(payload["sequence"], field="sequence")
    assert sequence is not None
    heartbeat = payload["heartbeat"]
    if not isinstance(heartbeat, bool):
        raise ValueError("heartbeat must be a boolean")

    eta_value = payload["eta_seconds"]
    eta_seconds = None if eta_value is None else _number(eta_value, field="eta_seconds")
    progress: dict[str, Any] = {
        "kind": "structured_progress",
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "scope": _token(payload["scope"], field="scope"),
        "stage": _token(payload["stage"], field="stage"),
        "current": current,
        "total": total,
        "unit": _token(payload["unit"], field="unit"),
        "elapsed_seconds": _number(payload["elapsed_seconds"], field="elapsed_seconds"),
        "eta_seconds": eta_seconds,
        "sequence": sequence,
        "reported_at": _reported_at(payload["reported_at"]),
        "heartbeat": heartbeat,
    }
    if current is not None and total is not None:
        progress["percent"] = 100.0 * (current / total)
    if "detail" in payload:
        progress["detail"] = _detail(payload["detail"])
    return progress


def parse_progress(log_tail: str) -> dict[str, Any] | None:
    encoded_events = [
        line[len(PROGRESS_PREFIX) :]
        for line in log_tail.splitlines()
        if line.startswith(PROGRESS_PREFIX)
    ]
    if not encoded_events:
        return None
    try:
        return decode_progress_event(encoded_events[-1])
    except ValueError as exc:
        return {
            "kind": "invalid_progress",
            "error": str(exc)[:_MAX_ERROR_LENGTH],
        }
