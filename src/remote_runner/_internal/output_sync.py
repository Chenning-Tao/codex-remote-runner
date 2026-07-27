from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from . import output_sync_remote
from .experiment_contracts import normalize_run_binding
from .result_metadata import (
    LEGACY_RESULT_INTENT,
    normalize_result_intent,
    normalize_result_tags,
)


RUN_ID_RE = re.compile(r"^rr-[0-9a-f]{16}$")
SERVER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
CONFIG_SCHEMA = 1
INTENT_SCHEMA = 1

PURGE_TARGET_PROGRAM = r"""import json
import re
import shutil
import fcntl
from pathlib import Path, PurePosixPath

payload = json.loads(PAYLOAD_JSON)
run_id = payload.get("run_id")
root_text = payload.get("target_root")
if not isinstance(run_id, str) or not re.fullmatch(r"rr-[0-9a-f]{16}", run_id):
    raise ValueError("invalid output-sync purge run id")
if not isinstance(root_text, str):
    raise ValueError("invalid output-sync purge target root")
root_posix = PurePosixPath(root_text)
if not root_posix.is_absolute() or str(root_posix) != root_text or ".." in root_posix.parts:
    raise ValueError("output-sync purge target root must be normalized and absolute")
root = Path(root_text)
if root.is_symlink() or (root.exists() and root.resolve() != root):
    raise ValueError("output-sync purge target root traverses a symlink")
target = root / "artifacts" / run_id
stage = root / ".staging" / (run_id + ".partial")
receipt = root / "receipts" / (run_id + ".json")
root.mkdir(parents=True, exist_ok=True, mode=0o700)
lock_path = root / ".output-sync.lock"
actions = {}
with lock_path.open("w", encoding="utf-8") as lock_handle:
    fcntl.flock(lock_handle, fcntl.LOCK_EX)
    for name, path in (("artifact", target), ("staging", stage)):
        if path.is_symlink():
            raise ValueError(name + " purge path is a symlink")
        if path.is_dir():
            shutil.rmtree(path)
            actions[name] = "removed"
        elif path.exists():
            raise ValueError(name + " purge path is not a directory")
        else:
            actions[name] = "already_absent"
    if receipt.is_symlink():
        raise ValueError("output-sync receipt is a symlink")
    if receipt.is_file():
        value = json.loads(receipt.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            raise ValueError("output-sync receipt identity mismatch")
        receipt.unlink()
        actions["receipt"] = "removed"
    elif receipt.exists():
        raise ValueError("output-sync receipt path is not a file")
    else:
        actions["receipt"] = "already_absent"
print(json.dumps({"ok": True, "run_id": run_id, "actions": actions}, sort_keys=True))
"""


@dataclass(frozen=True)
class OutputSyncPaths:
    root: Path
    config_path: Path
    pending_dir: Path
    completed_dir: Path
    state_dir: Path
    lock_path: Path


@dataclass(frozen=True)
class OutputSyncConfig:
    target_server: str
    target_ssh: str
    target_root: str
    target_python: str
    source_ssh_config: str
    source_hosts: dict[str, str]
    prune_source_servers: tuple[str, ...]
    restricted_source_keys: bool
    retry_seconds: int
    paused: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIG_SCHEMA,
            "target_server": self.target_server,
            "target_ssh": self.target_ssh,
            "target_root": self.target_root,
            "target_python": self.target_python,
            "source_ssh_config": self.source_ssh_config,
            "source_hosts": dict(sorted(self.source_hosts.items())),
            "prune_after_sync": {"servers": list(self.prune_source_servers)},
            "restricted_source_keys": self.restricted_source_keys,
            "retry_seconds": self.retry_seconds,
            "paused": self.paused,
        }


def output_sync_paths(registry_root: Path) -> OutputSyncPaths:
    root = registry_root / "output-sync"
    return OutputSyncPaths(
        root=root,
        config_path=root / "config.json",
        pending_dir=root / "pending",
        completed_dir=root / "completed",
        state_dir=root / "state",
        lock_path=root / "worker.lock",
    )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"output-sync {field} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"output-sync {field} must be a single-line string")
    return value


def _absolute_path(value: Any, field: str) -> str:
    text = _text(value, field)
    path = PurePosixPath(text)
    if not path.is_absolute() or str(path) != text or ".." in path.parts:
        raise ValueError(
            f"output-sync {field} must be a normalized absolute POSIX path"
        )
    return text


def validate_config_payload(raw: Any) -> OutputSyncConfig:
    if not isinstance(raw, dict) or raw.get("schema_version") != CONFIG_SCHEMA:
        raise ValueError("unsupported output-sync config schema")
    target_server = _text(raw.get("target_server"), "target_server")
    if SERVER_RE.fullmatch(target_server) is None:
        raise ValueError("output-sync target_server contains unsafe characters")
    target_ssh = _text(raw.get("target_ssh"), "target_ssh")
    if target_ssh.startswith("-") or any(
        character.isspace() for character in target_ssh
    ):
        raise ValueError("output-sync target_ssh must be one SSH destination argument")
    source_hosts_raw = raw.get("source_hosts")
    if not isinstance(source_hosts_raw, dict):
        raise ValueError("output-sync source_hosts must be a mapping")
    source_hosts: dict[str, str] = {}
    for server, host in source_hosts_raw.items():
        if not isinstance(server, str) or SERVER_RE.fullmatch(server) is None:
            raise ValueError("output-sync source_hosts has an invalid server name")
        host_text = _text(host, f"source_hosts.{server}")
        if SERVER_RE.fullmatch(host_text) is None:
            raise ValueError(
                f"output-sync source_hosts.{server} contains unsafe characters"
            )
        source_hosts[server] = host_text
    if target_server in source_hosts:
        raise ValueError("output-sync target_server must not appear in source_hosts")
    prune_after_sync = raw.get("prune_after_sync", {"servers": []})
    if not isinstance(prune_after_sync, dict):
        raise ValueError("output-sync prune_after_sync must be a mapping")
    prune_servers_raw = prune_after_sync.get("servers", [])
    if not isinstance(prune_servers_raw, list):
        raise ValueError("output-sync prune_after_sync.servers must be a list")
    prune_source_servers: list[str] = []
    for server in prune_servers_raw:
        if not isinstance(server, str) or SERVER_RE.fullmatch(server) is None:
            raise ValueError(
                "output-sync prune_after_sync.servers has an invalid server name"
            )
        if server not in source_hosts:
            raise ValueError(
                "output-sync prune_after_sync.servers must name configured source hosts"
            )
        prune_source_servers.append(server)
    if len(set(prune_source_servers)) != len(prune_source_servers):
        raise ValueError(
            "output-sync prune_after_sync.servers must not contain duplicates"
        )
    retry_seconds = raw.get("retry_seconds", 60)
    if (
        isinstance(retry_seconds, bool)
        or not isinstance(retry_seconds, int)
        or retry_seconds <= 0
    ):
        raise ValueError("output-sync retry_seconds must be a positive integer")
    paused = raw.get("paused", False)
    if not isinstance(paused, bool):
        raise ValueError("output-sync paused must be boolean")
    restricted_source_keys = raw.get("restricted_source_keys", False)
    if not isinstance(restricted_source_keys, bool):
        raise ValueError("output-sync restricted_source_keys must be boolean")
    return OutputSyncConfig(
        target_server=target_server,
        target_ssh=target_ssh,
        target_root=_absolute_path(raw.get("target_root"), "target_root"),
        target_python=_absolute_path(raw.get("target_python"), "target_python"),
        source_ssh_config=_absolute_path(
            raw.get("source_ssh_config"), "source_ssh_config"
        ),
        source_hosts=source_hosts,
        prune_source_servers=tuple(sorted(prune_source_servers)),
        restricted_source_keys=restricted_source_keys,
        retry_seconds=retry_seconds,
        paused=paused,
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _private_directory(path.parent)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
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
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid output-sync JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"output-sync JSON must be an object: {path}")
    return value


def store_config(registry_root: Path, payload: dict[str, Any]) -> OutputSyncConfig:
    config = validate_config_payload(payload)
    paths = output_sync_paths(registry_root)
    _private_directory(paths.root)
    _write_json_atomic(paths.config_path, config.to_payload())
    return config


def load_config(registry_root: Path) -> OutputSyncConfig | None:
    path = output_sync_paths(registry_root).config_path
    if not path.is_file():
        return None
    return validate_config_payload(_read_json(path))


def disable_config(registry_root: Path) -> bool:
    path = output_sync_paths(registry_root).config_path
    if not path.is_file():
        return False
    path.unlink()
    _fsync_directory(path.parent)
    return True


def is_configured(registry_root: Path) -> bool:
    return output_sync_paths(registry_root).config_path.is_file()


def _intent_from_manifest(
    manifest: dict[str, Any],
    *,
    state_revision: int,
    succeeded_at: str,
) -> dict[str, Any] | None:
    output_path = manifest.get("output_path")
    if output_path is None:
        return None
    run_id = str(manifest.get("run_id"))
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("output-sync intent requires a current run id")
    metadata = manifest.get("output_metadata")
    if not isinstance(metadata, dict):
        raise ValueError("output-sync intent output_metadata must be a mapping")
    revision = manifest.get("source_revision") or manifest.get("expected_revision")
    if not isinstance(revision, str) or not revision:
        revision = "unknown"
    intent = {
        "schema_version": INTENT_SCHEMA,
        "run_id": run_id,
        "source_server": str(manifest["server"]),
        "source_path": str(output_path),
        "revision": revision,
        "task_id": str(manifest["task_id"]),
        "label": str(manifest["label"]),
        "result_intent": normalize_result_intent(
            manifest.get("result_intent", LEGACY_RESULT_INTENT),
            allow_unclassified=True,
            field="output-sync result_intent",
        ),
        "result_tags": normalize_result_tags(
            manifest.get("result_tags", {}), field="output-sync result_tags"
        ),
        "output_metadata": metadata,
        "succeeded_at": succeeded_at,
        "state_revision": state_revision,
    }
    raw_binding = manifest.get("experiment_binding")
    if raw_binding is not None:
        binding = normalize_run_binding(raw_binding)
        if binding["run_id"] != run_id or binding["source_revision"] != revision:
            raise ValueError("output-sync experiment binding identity mismatch")
        intent["experiment_binding"] = binding
    return intent


def enqueue_succeeded_output(
    registry_root: Path,
    manifest: dict[str, Any],
    *,
    state_revision: int,
    succeeded_at: str,
) -> Path | None:
    intent = _intent_from_manifest(
        manifest,
        state_revision=state_revision,
        succeeded_at=succeeded_at,
    )
    if intent is None:
        return None
    paths = output_sync_paths(registry_root)
    completed = paths.completed_dir / f"{intent['run_id']}.json"
    pending = paths.pending_dir / f"{intent['run_id']}.json"
    if completed.is_file():
        return completed
    if pending.is_file():
        existing = _read_json(pending)
        if existing != intent:
            raise ValueError(f"output-sync intent conflict for {intent['run_id']}")
        return pending
    _write_json_atomic(pending, intent)
    return pending


def validate_intent(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != INTENT_SCHEMA:
        raise ValueError("unsupported output-sync intent schema")
    run_id = _text(raw.get("run_id"), "intent.run_id")
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("output-sync intent has invalid run_id")
    source_server = _text(raw.get("source_server"), "intent.source_server")
    if SERVER_RE.fullmatch(source_server) is None:
        raise ValueError("output-sync intent has invalid source_server")
    _absolute_path(raw.get("source_path"), "intent.source_path")
    for field in ("revision", "task_id", "label", "succeeded_at"):
        _text(raw.get(field), f"intent.{field}")
    raw["result_intent"] = normalize_result_intent(
        raw.get("result_intent", LEGACY_RESULT_INTENT),
        allow_unclassified=True,
        field="intent.result_intent",
    )
    raw["result_tags"] = normalize_result_tags(
        raw.get("result_tags", {}), field="intent.result_tags"
    )
    if not isinstance(raw.get("output_metadata"), dict):
        raise ValueError("output-sync intent output_metadata must be a mapping")
    raw_binding = raw.get("experiment_binding")
    if raw_binding is not None:
        binding = normalize_run_binding(raw_binding)
        if binding["run_id"] != run_id or binding["source_revision"] != raw["revision"]:
            raise ValueError("output-sync intent experiment binding identity mismatch")
        raw["experiment_binding"] = binding
    state_revision = raw.get("state_revision")
    if (
        isinstance(state_revision, bool)
        or not isinstance(state_revision, int)
        or state_revision < 1
    ):
        raise ValueError("output-sync intent state_revision must be positive")
    return dict(raw)


def list_pending(registry_root: Path) -> list[dict[str, Any]]:
    directory = output_sync_paths(registry_root).pending_dir
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("rr-*.json")):
        rows.append(validate_intent(_read_json(path)))
    return rows


def has_pending(registry_root: Path) -> bool:
    directory = output_sync_paths(registry_root).pending_dir
    return directory.is_dir() and any(directory.glob("rr-*.json"))


@contextlib.contextmanager
def worker_lock(registry_root: Path, *, nonblocking: bool = False) -> Iterator[None]:
    paths = output_sync_paths(registry_root)
    _private_directory(paths.root)
    descriptor = os.open(paths.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(descriptor, operation)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _attempt_state(registry_root: Path, run_id: str) -> dict[str, Any]:
    path = output_sync_paths(registry_root).state_dir / f"{run_id}.json"
    if not path.is_file():
        return {"attempts": 0}
    return _read_json(path)


def _record_attempt(
    registry_root: Path,
    run_id: str,
    *,
    status: str,
    error: str | None,
) -> None:
    state = _attempt_state(registry_root, run_id)
    attempts = int(state.get("attempts", 0)) + (status == "retryable")
    _write_json_atomic(
        output_sync_paths(registry_root).state_dir / f"{run_id}.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "status": status,
            "attempts": attempts,
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "last_error": error,
        },
    )


def _remote_program() -> str:
    path = Path(str(output_sync_remote.__file__))
    if not path.is_file():
        raise RuntimeError("installed output-sync remote program source is unavailable")
    return path.read_text(encoding="utf-8")


def invoke_target(
    config: OutputSyncConfig,
    intent: dict[str, Any],
    *,
    connect_timeout: int,
) -> dict[str, Any]:
    source_server = str(intent["source_server"])
    if source_server == config.target_server:
        source_host = None
    else:
        source_host = config.source_hosts.get(source_server)
        if source_host is None:
            raise ValueError(
                f"output-sync target {config.target_server!r} has no source host "
                f"for server {source_server!r}"
            )
    payload = {
        "schema_version": 1,
        "run_id": intent["run_id"],
        "source_server": source_server,
        "source_host": source_host,
        "source_path": intent["source_path"],
        "target_server": config.target_server,
        "target_root": config.target_root,
        "source_ssh_config": config.source_ssh_config,
        "restricted_source_keys": config.restricted_source_keys,
        "revision": intent["revision"],
        "task_id": intent["task_id"],
        "label": intent["label"],
        "result_intent": intent["result_intent"],
        "result_tags": intent["result_tags"],
        "output_metadata": intent["output_metadata"],
        "succeeded_at": intent["succeeded_at"],
    }
    if intent.get("experiment_binding") is not None:
        payload["experiment_binding"] = intent["experiment_binding"]
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    remote_command = " ".join(
        (
            f"{output_sync_remote.PAYLOAD_ENV}={shlex.quote(encoded)}",
            shlex.quote(config.target_python),
            "-",
        )
    )
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            config.target_ssh,
            remote_command,
        ],
        input=_remote_program(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{config.target_server} output-sync worker returned invalid JSON"
        ) from exc
    if (
        completed.returncode != 0
        or not isinstance(result, dict)
        or result.get("ok") is not True
    ):
        detail = (
            result.get("error")
            if isinstance(result, dict) and isinstance(result.get("error"), str)
            else completed.stderr.strip()
            or f"{config.target_server} output-sync worker failed"
        )
        raise RuntimeError(detail)
    receipt = result.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("run_id") != intent["run_id"]:
        raise RuntimeError(
            f"{config.target_server} output-sync worker returned an invalid receipt"
        )
    return receipt


def _purge_target_run(
    config: OutputSyncConfig,
    run_id: str,
    *,
    connect_timeout: int,
) -> dict[str, Any]:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("invalid output-sync purge run id")
    payload = json.dumps(
        {"run_id": run_id, "target_root": config.target_root},
        sort_keys=True,
        separators=(",", ":"),
    )
    program = f"PAYLOAD_JSON = {payload!r}\n" + PURGE_TARGET_PROGRAM
    try:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"ConnectTimeout={connect_timeout}",
                config.target_ssh,
                f"{shlex.quote(config.target_python)} -",
            ],
            input=program,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=connect_timeout + 300,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"output-sync purge target timed out after {exc.timeout}s; outcome is unknown"
        ) from exc
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as exc:
        raise RuntimeError("output-sync purge target returned invalid JSON") from exc
    if (
        completed.returncode != 0
        or not isinstance(result, dict)
        or result.get("ok") is not True
    ):
        raise RuntimeError(
            completed.stderr.strip() or "output-sync purge target failed"
        )
    return result


def purge_run_sync_state(
    registry_root: Path,
    run_ids: set[str],
    *,
    target_configs: dict[str, dict[str, Any]],
    connect_timeout: int,
) -> list[dict[str, Any]]:
    invalid = sorted(
        run_id for run_id in run_ids if RUN_ID_RE.fullmatch(run_id) is None
    )
    if invalid:
        raise ValueError(f"invalid output-sync purge run ids: {', '.join(invalid)}")
    if not set(target_configs).issubset(run_ids):
        raise ValueError("output-sync target purge ids must be part of the task purge")
    paths = output_sync_paths(registry_root)
    validated_configs = {
        run_id: validate_config_payload(payload)
        for run_id, payload in target_configs.items()
    }
    results: list[dict[str, Any]] = []
    with worker_lock(registry_root):
        for run_id in sorted(run_ids):
            result: dict[str, Any] = {"run_id": run_id}
            config = validated_configs.get(run_id)
            if config is not None:
                result["target"] = _purge_target_run(
                    config,
                    run_id,
                    connect_timeout=connect_timeout,
                )
            removed: list[str] = []
            for name, directory in (
                ("pending", paths.pending_dir),
                ("completed", paths.completed_dir),
                ("state", paths.state_dir),
            ):
                path = directory / f"{run_id}.json"
                if path.is_symlink():
                    raise ValueError(f"output-sync {name} path is a symlink: {run_id}")
                if path.is_file():
                    path.unlink()
                    removed.append(name)
                    _fsync_directory(directory)
                elif path.exists():
                    raise ValueError(
                        f"output-sync {name} path is not a regular file: {run_id}"
                    )
            result["removed_controller_state"] = removed
            results.append(result)
    return results


def _failed_run_sync_state_unlocked(
    paths: OutputSyncPaths,
    run_id: str,
) -> dict[str, Any]:
    evidence: list[str] = []
    for name, directory in (
        ("pending", paths.pending_dir),
        ("state", paths.state_dir),
    ):
        path = directory / f"{run_id}.json"
        if path.is_symlink():
            raise ValueError(f"output-sync {name} path is a symlink: {run_id}")
        if path.is_file():
            evidence.append(name)
        elif path.exists():
            raise ValueError(f"output-sync {name} path is not a regular file: {run_id}")

    completed = paths.completed_dir / f"{run_id}.json"
    if completed.is_symlink():
        raise ValueError(f"output-sync completed path is a symlink: {run_id}")
    if completed.is_file():
        value = _read_json(completed)
        receipt = value.get("receipt")
        if (
            not isinstance(receipt, dict)
            or receipt.get("run_id") != run_id
            or receipt.get("disposition") != "cancelled_before_sync"
            or receipt.get("authoritative_status") != "failed"
        ):
            raise ValueError(
                "failed run has output-sync evidence that is not a failed-state "
                "cancellation"
            )
        evidence.append("completed_cancelled")
    elif completed.exists():
        raise ValueError(f"output-sync completed path is not a regular file: {run_id}")
    return {"run_id": run_id, "evidence": evidence}


def inspect_failed_run_sync_state(
    registry_root: Path,
    run_id: str,
) -> dict[str, Any]:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("invalid failed-run output-sync id")
    paths = output_sync_paths(registry_root)
    return _failed_run_sync_state_unlocked(paths, run_id)


def purge_failed_run_sync_state(
    registry_root: Path,
    run_id: str,
) -> dict[str, Any]:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("invalid failed-run output-sync id")
    paths = output_sync_paths(registry_root)
    with worker_lock(registry_root):
        result = _failed_run_sync_state_unlocked(paths, run_id)
        removed: list[str] = []
        for name, directory in (
            ("pending", paths.pending_dir),
            ("completed", paths.completed_dir),
            ("state", paths.state_dir),
        ):
            path = directory / f"{run_id}.json"
            if path.is_file():
                path.unlink()
                removed.append(name)
                _fsync_directory(directory)
        result["removed_controller_state"] = removed
        return result


def _complete_intent(
    registry_root: Path,
    intent: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    paths = output_sync_paths(registry_root)
    run_id = str(intent["run_id"])
    completed = {
        "schema_version": 1,
        "run_id": run_id,
        "intent": intent,
        "receipt": receipt,
        "confirmed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    _write_json_atomic(paths.completed_dir / f"{run_id}.json", completed)
    pending = paths.pending_dir / f"{run_id}.json"
    with contextlib.suppress(FileNotFoundError):
        pending.unlink()
        _fsync_directory(paths.pending_dir)
    state = paths.state_dir / f"{run_id}.json"
    with contextlib.suppress(FileNotFoundError):
        state.unlink()
        _fsync_directory(paths.state_dir)


def process_pending_once(
    execution_paths: Any,
    *,
    connect_timeout: int,
) -> dict[str, Any]:
    from .execution_registry import load_current_run, registry_kind

    config = load_config(execution_paths.registry_root)
    if config is None:
        return {
            "enabled": False,
            "pending": len(list_pending(execution_paths.registry_root)),
        }
    if config.paused:
        return {
            "enabled": True,
            "paused": True,
            "processed": 0,
            "remaining": len(list_pending(execution_paths.registry_root)),
            "results": [],
        }
    results: list[dict[str, Any]] = []
    with worker_lock(execution_paths.registry_root):
        for intent in list_pending(execution_paths.registry_root):
            run_id = str(intent["run_id"])
            result: dict[str, Any] = {"run_id": run_id}
            try:
                if registry_kind(execution_paths, run_id) != "current":
                    raise ValueError("output-sync run is not a current registry record")
                manifest, state = load_current_run(execution_paths, run_id)
                if state["status"] in {"failed", "stopped"}:
                    receipt = {
                        "schema_version": 1,
                        "run_id": run_id,
                        "disposition": "cancelled_before_sync",
                        "authoritative_status": state["status"],
                        "source_deletion_performed": False,
                    }
                    _complete_intent(execution_paths.registry_root, intent, receipt)
                    result["status"] = "cancelled"
                    results.append(result)
                    continue
                if state["status"] != "succeeded":
                    _record_attempt(
                        execution_paths.registry_root,
                        run_id,
                        status="waiting_for_succeeded_state",
                        error=None,
                    )
                    result["status"] = "waiting"
                    results.append(result)
                    continue
                if manifest.get("output_path") != intent["source_path"]:
                    raise ValueError("output-sync source path changed after enqueue")
                if manifest.get("server") != intent["source_server"]:
                    raise ValueError("output-sync source server changed after enqueue")
                receipt = invoke_target(
                    config,
                    intent,
                    connect_timeout=connect_timeout,
                )
                _complete_intent(execution_paths.registry_root, intent, receipt)
                result.update({"status": "archived", "receipt": receipt})
            except (OSError, RuntimeError, ValueError) as exc:
                _record_attempt(
                    execution_paths.registry_root,
                    run_id,
                    status="retryable",
                    error=str(exc),
                )
                result.update({"status": "retryable", "error": str(exc)})
            results.append(result)
    return {
        "enabled": True,
        "paused": False,
        "processed": len(results),
        "archived": sum(item["status"] == "archived" for item in results),
        "retryable": sum(item["status"] == "retryable" for item in results),
        "waiting": sum(item["status"] == "waiting" for item in results),
        "cancelled": sum(item["status"] == "cancelled" for item in results),
        "remaining": len(list_pending(execution_paths.registry_root)),
        "results": results,
    }


def sync_status(registry_root: Path) -> dict[str, Any]:
    paths = output_sync_paths(registry_root)
    config = load_config(registry_root)
    states: list[dict[str, Any]] = []
    if paths.state_dir.is_dir():
        for path in sorted(paths.state_dir.glob("rr-*.json")):
            states.append(_read_json(path))
    return {
        "enabled": config is not None,
        "paused": None if config is None else config.paused,
        "pending": len(list_pending(registry_root)),
        "completed": (
            len(list(paths.completed_dir.glob("rr-*.json")))
            if paths.completed_dir.is_dir()
            else 0
        ),
        "retryable": sum(item.get("status") == "retryable" for item in states),
        "waiting": sum(
            item.get("status") == "waiting_for_succeeded_state" for item in states
        ),
    }


def run_sync_status(registry_root: Path, run_id: str) -> dict[str, Any]:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("invalid output-sync run id")
    paths = output_sync_paths(registry_root)
    completed_path = paths.completed_dir / f"{run_id}.json"
    if completed_path.is_symlink():
        raise ValueError(f"output-sync completed path is a symlink: {run_id}")
    if completed_path.is_file():
        completed = _read_json(completed_path)
        receipt = completed.get("receipt")
        if completed.get("run_id") != run_id or not isinstance(receipt, dict):
            raise ValueError(f"completed output-sync receipt is invalid: {run_id}")
        status = (
            "cancelled"
            if receipt.get("disposition") == "cancelled_before_sync"
            else "completed"
        )
        return {
            "status": status,
            "confirmed_at": completed.get("confirmed_at"),
            "receipt": receipt,
        }

    pending_path = paths.pending_dir / f"{run_id}.json"
    if pending_path.is_symlink():
        raise ValueError(f"output-sync pending path is a symlink: {run_id}")
    if not pending_path.is_file():
        return {"status": "not_enqueued"}
    intent = validate_intent(_read_json(pending_path))
    attempt = _attempt_state(registry_root, run_id)
    attempt_status = attempt.get("status")
    status = (
        str(attempt_status)
        if attempt_status in {"retryable", "waiting_for_succeeded_state"}
        else "pending"
    )
    return {
        "status": status,
        "source_server": intent["source_server"],
        "source_path": intent["source_path"],
        "attempts": int(attempt.get("attempts", 0)),
        "updated_at": attempt.get("updated_at"),
        "last_error": attempt.get("last_error"),
    }


def list_completed_syncs(registry_root: Path) -> list[dict[str, Any]]:
    directory = output_sync_paths(registry_root).completed_dir
    if not directory.is_dir():
        return []
    completed: list[dict[str, Any]] = []
    for path in sorted(directory.glob("rr-*.json")):
        if path.is_symlink():
            raise ValueError(f"output-sync completed path is a symlink: {path.name}")
        value = _read_json(path)
        run_id = value.get("run_id")
        if not isinstance(run_id, str) or path.name != f"{run_id}.json":
            raise ValueError(f"output-sync completed identity mismatch: {path.name}")
        completed.append(value)
    return completed


def has_unpruned_completed_syncs(
    registry_root: Path,
    source_servers: tuple[str, ...],
) -> bool:
    if not source_servers:
        return False
    selected = set(source_servers)
    for completed in list_completed_syncs(registry_root):
        intent = completed.get("intent")
        receipt = completed.get("receipt")
        if not isinstance(intent, dict) or not isinstance(receipt, dict):
            continue
        if intent.get("source_server") not in selected:
            continue
        if receipt.get("verification") != "rsync_checksum_dry_run":
            continue
        if receipt.get("source_deletion_performed") is False:
            return True
    return False


def record_source_output_deletion(
    registry_root: Path,
    run_id: str,
    *,
    deletion_result: dict[str, Any],
) -> dict[str, Any]:
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("invalid output-sync source deletion run id")
    paths = output_sync_paths(registry_root)
    with worker_lock(registry_root):
        path = paths.completed_dir / f"{run_id}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"completed output-sync receipt is unavailable: {run_id}")
        completed = _read_json(path)
        receipt = completed.get("receipt")
        if not isinstance(receipt, dict) or receipt.get("run_id") != run_id:
            raise ValueError(f"completed output-sync receipt is invalid: {run_id}")
        if receipt.get("source_deletion_performed") is True:
            return completed
        updated_receipt = {
            **receipt,
            "source_deletion_performed": True,
            "source_deleted_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "source_deletion_result": dict(deletion_result),
        }
        updated = {**completed, "receipt": updated_receipt}
        _write_json_atomic(path, updated)
        return updated
