from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import shlex
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


PAYLOAD_ENV = "REMOTE_RUNNER_OUTPUT_SYNC_PAYLOAD"
RUN_ID_RE = re.compile(r"^rr-[0-9a-f]{16}$")
HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")
REMOTE_KIND_PROGRAM = r"""
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.is_symlink():
    kind = "symlink"
elif path.is_dir():
    kind = "directory"
elif path.is_file():
    kind = "file"
else:
    kind = "missing"
print(json.dumps({"kind": kind}, separators=(",", ":")))
"""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field} must be a single-line string")
    return value


def _absolute_path(value: Any, field: str) -> str:
    text = _text(value, field)
    path = PurePosixPath(text)
    if not path.is_absolute() or str(path) != text or ".." in path.parts:
        raise ValueError(f"{field} must be a normalized absolute POSIX path")
    return text


def validate_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("unsupported output-sync payload")
    payload = dict(raw)
    run_id = _text(payload.get("run_id"), "run_id")
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_id must match rr-<16 lowercase hex>")
    for field in ("source_server", "target_server"):
        value = _text(payload.get(field), field)
        if HOST_RE.fullmatch(value) is None:
            raise ValueError(f"{field} contains unsafe characters")
    source_host = payload.get("source_host")
    if source_host is not None:
        source_host = _text(source_host, "source_host")
        if HOST_RE.fullmatch(source_host) is None:
            raise ValueError("source_host contains unsafe characters")
    if payload["source_server"] == payload["target_server"]:
        if source_host is not None:
            raise ValueError("local target source must not provide source_host")
    elif source_host is None:
        raise ValueError("remote source requires source_host")
    payload["source_host"] = source_host
    payload["source_path"] = _absolute_path(payload.get("source_path"), "source_path")
    payload["target_root"] = _absolute_path(payload.get("target_root"), "target_root")
    payload["source_ssh_config"] = _absolute_path(
        payload.get("source_ssh_config"), "source_ssh_config"
    )
    restricted_source_keys = payload.get("restricted_source_keys", False)
    if not isinstance(restricted_source_keys, bool):
        raise ValueError("restricted_source_keys must be boolean")
    payload["restricted_source_keys"] = restricted_source_keys
    if payload["source_server"] == payload["target_server"]:
        source = PurePosixPath(payload["source_path"])
        target = PurePosixPath(payload["target_root"])
        if source == target or source in target.parents or target in source.parents:
            raise ValueError("local source_path and target_root must not overlap")
    for field in ("revision", "task_id", "label", "terminal_at"):
        _text(payload.get(field), field)
    metadata = payload.get("output_metadata")
    if not isinstance(metadata, dict):
        raise ValueError("output_metadata must be a mapping")
    if payload.get("authoritative_status") not in {"succeeded", "failed", "stopped"}:
        raise ValueError("authoritative_status must be terminal")
    return payload


def _load_payload() -> dict[str, Any]:
    encoded = os.environ.get(PAYLOAD_ENV)
    if not encoded:
        raise ValueError(f"{PAYLOAD_ENV} is required")
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        value = json.loads(raw)
    except (UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid encoded output-sync payload") from exc
    return validate_payload(value)


def _local_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "missing"


def _source_kind(payload: dict[str, Any]) -> str:
    if payload["source_host"] is None:
        kind = _local_kind(Path(payload["source_path"]))
    else:
        remote_command = shlex.join(
            ["python3", "-c", REMOTE_KIND_PROGRAM, payload["source_path"]]
        )
        completed = subprocess.run(
            [
                "ssh",
                "-F",
                payload["source_ssh_config"],
                "-o",
                "BatchMode=yes",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=3",
                payload["source_host"],
                remote_command,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "source probe failed: " + (completed.stderr.strip() or "unknown error")
            )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("source probe returned invalid JSON") from exc
        kind = result.get("kind") if isinstance(result, dict) else None
    if kind not in {"directory", "file"}:
        raise ValueError(
            f"source output must be a regular file or directory, found {kind!r}"
        )
    return str(kind)


def build_rsync_command(
    payload: dict[str, Any],
    *,
    destination: Path,
    source_kind: str,
    verify_only: bool,
) -> list[str]:
    source_path = payload["source_path"]
    if source_kind == "directory":
        source_path = source_path.rstrip("/") + "/"
    source = (
        source_path
        if payload["source_host"] is None
        else f"{payload['source_host']}:{source_path}"
    )
    command = [
        "rsync",
        "-a",
        "--checksum",
        "--delete",
        "--timeout=300",
    ]
    if not payload["restricted_source_keys"]:
        command.append("--protect-args")
    if verify_only:
        command.extend(["--dry-run", "--itemize-changes"])
    else:
        command.extend(["--partial", "--stats"])
    if payload["source_host"] is not None:
        remote_shell = shlex.join(
            [
                "ssh",
                "-F",
                payload["source_ssh_config"],
                "-o",
                "BatchMode=yes",
                "-o",
                "ServerAliveInterval=30",
                "-o",
                "ServerAliveCountMax=3",
            ]
        )
        command.extend(["-e", remote_shell])
    command.extend([source, str(destination) + "/"])
    return command


def _run_rsync(
    payload: dict[str, Any],
    *,
    destination: Path,
    source_kind: str,
    verify_only: bool,
) -> None:
    command = build_rsync_command(
        payload,
        destination=destination,
        source_kind=source_kind,
        verify_only=verify_only,
    )
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"rsync {'verification' if verify_only else 'copy'} failed: {detail}"
        )
    if verify_only:
        changes = [line for line in completed.stdout.splitlines() if line.strip()]
        if changes:
            raise ValueError(
                "rsync checksum verification found differences: "
                + " | ".join(changes[:10])
            )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_file_stage(stage: Path, source_path: str) -> None:
    expected = Path(source_path).name
    entries = sorted(item.name for item in stage.iterdir())
    if entries != [expected]:
        raise ValueError(
            f"file output staging contains unexpected entries: expected {[expected]!r}, "
            f"found {entries!r}"
        )


def sync_payload(raw: Any) -> dict[str, Any]:
    payload = validate_payload(raw)
    target_root = Path(payload["target_root"])
    artifacts_root = target_root / "artifacts"
    staging_root = target_root / ".staging"
    receipts_root = target_root / "receipts"
    target = artifacts_root / payload["run_id"]
    stage = staging_root / f"{payload['run_id']}.partial"

    for directory in (target_root, artifacts_root, staging_root, receipts_root):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = target_root / ".output-sync.lock"
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        source_kind = _source_kind(payload)
        if target.is_symlink() or stage.is_symlink():
            raise ValueError("artifact target and staging paths must not be symlinks")
        if target.exists():
            if not target.is_dir():
                raise ValueError("existing artifact target is not a directory")
            _run_rsync(
                payload,
                destination=target,
                source_kind=source_kind,
                verify_only=True,
            )
            if source_kind == "file":
                _validate_file_stage(target, payload["source_path"])
            disposition = "already_present_verified"
        else:
            stage.mkdir(parents=True, exist_ok=True, mode=0o700)
            _run_rsync(
                payload,
                destination=stage,
                source_kind=source_kind,
                verify_only=False,
            )
            _run_rsync(
                payload,
                destination=stage,
                source_kind=source_kind,
                verify_only=True,
            )
            if source_kind == "file":
                _validate_file_stage(stage, payload["source_path"])
            stage.rename(target)
            _fsync_directory(artifacts_root)
            disposition = "copied_and_verified"

        receipt = {
            "schema_version": 1,
            "run_id": payload["run_id"],
            "source_server": payload["source_server"],
            "source_path": payload["source_path"],
            "source_kind": source_kind,
            "target_path": str(target),
            "revision": payload["revision"],
            "task_id": payload["task_id"],
            "authoritative_status": payload["authoritative_status"],
            "terminal_at": payload["terminal_at"],
            "archived_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "verification": "rsync_checksum_dry_run",
            "disposition": disposition,
            "source_deletion_performed": False,
        }
        _write_json_atomic(receipts_root / f"{payload['run_id']}.json", receipt)
        return receipt


def main() -> int:
    try:
        receipt = sync_payload(_load_payload())
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "receipt": receipt}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
