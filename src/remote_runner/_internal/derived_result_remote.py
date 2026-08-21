"""Bounded reader for one small result file inside a validator artifact.

This program runs on the archive target with the project's configured Python and
returns raw bytes only. It never interprets the payload: the caller verifies the
digest and parses the JSON. Every guard here exists so that a workload cannot use
its own artifact to make Remote Runner read something else on the machine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any


PAYLOAD_ENV = "REMOTE_RUNNER_DERIVED_RESULT_PAYLOAD"
MAX_RESULT_BYTES = 1024 * 1024


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field} contains invalid control characters")
    return value


def validate_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("result payload must be a mapping")
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported result payload schema")
    root = PurePosixPath(_text(raw.get("artifact_root"), "artifact_root"))
    if not root.is_absolute() or str(root) != raw["artifact_root"]:
        raise ValueError("artifact_root must be a normalized absolute POSIX path")
    relpath = PurePosixPath(_text(raw.get("relpath"), "relpath"))
    if relpath.is_absolute() or str(relpath) != raw["relpath"]:
        raise ValueError("relpath must be a normalized relative POSIX path")
    if any(part in {".", ".."} for part in relpath.parts):
        raise ValueError("relpath cannot contain dot or parent traversal components")
    max_bytes = raw.get("max_bytes", MAX_RESULT_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise ValueError("max_bytes must be an integer")
    if not 0 < max_bytes <= MAX_RESULT_BYTES:
        raise ValueError(f"max_bytes must be between 1 and {MAX_RESULT_BYTES}")
    return {
        "schema_version": 1,
        "artifact_root": str(root),
        "relpath": str(relpath),
        "max_bytes": max_bytes,
    }


def _load_payload() -> dict[str, Any]:
    encoded = os.environ.get(PAYLOAD_ENV)
    if not encoded:
        raise ValueError(f"{PAYLOAD_ENV} is required")
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        value = json.loads(raw)
    except (UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid encoded result payload") from exc
    return validate_payload(value)


def read_result(payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(payload["artifact_root"])
    if root.is_symlink():
        raise ValueError("validator artifact root must not be a symlink")
    if not root.is_dir():
        raise ValueError("validator artifact root is not a directory")
    current = root
    for part in PurePosixPath(payload["relpath"]).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"result path component is a symlink: {part}")
        if not current.exists():
            raise ValueError(f"result path component does not exist: {part}")
    if not current.is_file():
        raise ValueError("result path is not a regular file")
    if os.path.realpath(current) != str(current):
        raise ValueError("result path does not resolve inside the artifact root")
    size = current.stat().st_size
    max_bytes = int(payload["max_bytes"])
    if size > max_bytes:
        raise ValueError(f"result file is {size} bytes, above the {max_bytes} limit")
    with current.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("result file grew beyond the size limit while being read")
    return {
        "path": str(current),
        "size": len(content),
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def main() -> int:
    try:
        result = read_result(_load_payload())
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
