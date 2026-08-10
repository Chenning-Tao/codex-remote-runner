from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .output_sync import enqueue_terminal_output, is_configured
from .output_paths import validate_resolved_output
from .scheduling import normalize_workload_class

PROJECT_CONFIG_NAME = ".remote-runner.yaml"
CURRENT_MANIFEST_SCHEMA = 4
PREVIOUS_MANIFEST_SCHEMA = 3
CURRENT_STATE_SCHEMA = 2
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CURRENT_RUN_ID_RE = re.compile(r"^rr-[0-9a-f]{16}$")
AUTHORITATIVE_STATUSES = {"registered", "running", "succeeded", "failed", "stopped"}
TERMINAL_STATUSES = {"succeeded", "failed", "stopped"}
_FORWARD_STATUSES: dict[str, set[str]] = {
    "registered": {"running", "succeeded", "failed", "stopped"},
    "running": {"succeeded", "failed", "stopped"},
    "succeeded": set(),
    "failed": set(),
    "stopped": set(),
}
_FORBIDDEN_CORE_MANIFEST_FIELDS = {
    "assets",
    "launch_plan",
    "process_privacy",
    "privacy",
    "retention",
}
PROCESS_TITLE_PRIVACY_MODE = "process-title"
_PROCESS_TITLE_PRIVACY_MANIFEST = {"mode": "required"}


@dataclass(frozen=True)
class ProjectPaths:
    config_path: Path
    project_root: Path
    registry_root: Path
    runs_dir: Path
    locks_dir: Path
    events_path: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_run_id(run_id: str) -> str:
    """Validate a historical or current run identifier."""
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run_id must start with an alphanumeric character and contain only "
            "letters, digits, dots, underscores, or hyphens"
        )
    return run_id


def validate_current_run_id(run_id: str) -> str:
    if not CURRENT_RUN_ID_RE.fullmatch(run_id):
        raise ValueError("current run_id must match rr-<16 lowercase hex>")
    return run_id


def generate_run_id(*, runs_dir: Path, attempts: int = 128) -> str:
    for _ in range(attempts):
        run_id = f"rr-{secrets.token_hex(8)}"
        if not (runs_dir / run_id).exists() and not (runs_dir / f"{run_id}.yaml").exists():
            return run_id
    raise RuntimeError("could not allocate a unique run id")


def runtime_path(run_id: str) -> str:
    return f"~/.rr/{validate_current_run_id(run_id)}"


def remote_status_path_for_log(remote_log: str) -> str:
    if remote_log.endswith(".log"):
        return f"{remote_log[:-4]}.status.json"
    return f"{remote_log}.status.json"


def sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def resolve_project_config(
    project_config: Path | None = None,
    *,
    start: Path | None = None,
) -> Path:
    base = (start or Path.cwd()).expanduser().resolve()
    if project_config is not None:
        candidate = project_config.expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"project config does not exist: {candidate}")
        return candidate

    search_root = base if base.is_dir() else base.parent
    for directory in (search_root, *search_root.parents):
        candidate = directory / PROJECT_CONFIG_NAME
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        f"could not find {PROJECT_CONFIG_NAME} from {search_root} or its parents"
    )


def project_paths(config_path: Path) -> ProjectPaths:
    resolved = config_path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"project config does not exist: {resolved}")
    project_root = resolved.parent
    registry_root = project_root / ".remote-runner"
    return ProjectPaths(
        config_path=resolved,
        project_root=project_root,
        registry_root=registry_root,
        runs_dir=registry_root / "runs",
        locks_dir=registry_root / "locks",
        events_path=registry_root / "runs.jsonl",
    )


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for remote-runner YAML files") from exc

    try:
        with path.open("r", encoding="utf-8") as handle:
            loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
            data = yaml.load(handle, Loader=loader) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML document {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"YAML document must be a mapping: {path}")
    return data


def _yaml_bytes(data: dict[str, Any]) -> bytes:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for remote-runner YAML files") from exc
    return yaml.safe_dump(data, sort_keys=False).encode()


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("write returned zero bytes")
        view = view[written:]


def _write_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        os.fchmod(fd, mode)
        _write_all(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace one mutable private YAML file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(_yaml_bytes(data))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def replace_config_yaml(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace an existing user-managed YAML config."""
    replace_config_text(path, _yaml_bytes(data))


def replace_config_text(path: Path, content: str | bytes) -> None:
    """Atomically replace an existing user-managed text config."""
    resolved = path.expanduser().resolve(strict=True)
    mode = stat.S_IMODE(resolved.stat().st_mode)
    encoded = content.encode() if isinstance(content, str) else content
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{resolved.name}.", dir=resolved.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, resolved)
        _fsync_directory(resolved.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def current_run_dir(paths: ProjectPaths, run_id: str) -> Path:
    return paths.runs_dir / validate_current_run_id(run_id)


def current_manifest_path(paths: ProjectPaths, run_id: str) -> Path:
    return current_run_dir(paths, run_id) / "manifest.yaml"


def current_state_path(paths: ProjectPaths, run_id: str) -> Path:
    return current_run_dir(paths, run_id) / "state.yaml"


def current_command_path(paths: ProjectPaths, run_id: str) -> Path:
    return current_run_dir(paths, run_id) / "command.sh"


def legacy_manifest_path(paths: ProjectPaths, run_id: str) -> Path:
    return paths.runs_dir / f"{validate_run_id(run_id)}.yaml"


def registry_kind(paths: ProjectPaths, run_id: str) -> str | None:
    validate_run_id(run_id)
    legacy = legacy_manifest_path(paths, run_id).exists()
    directory = paths.runs_dir / run_id
    manifest_path = directory / "manifest.yaml"
    current_or_v2 = manifest_path.exists()
    if legacy and current_or_v2:
        return "conflict"
    if legacy:
        return "legacy"
    if not directory.exists():
        return None
    if not manifest_path.is_file():
        return "unsupported"
    try:
        schema = load_yaml(manifest_path).get("schema_version")
    except (OSError, RuntimeError, ValueError):
        return "unsupported"
    if schema in {PREVIOUS_MANIFEST_SCHEMA, CURRENT_MANIFEST_SCHEMA}:
        return "current"
    if schema == 2:
        return "v2"
    return "unsupported"


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def registry_lock(paths: ProjectPaths) -> Iterator[None]:
    with _file_lock(paths.locks_dir / "registry.lock"):
        yield


@contextlib.contextmanager
def event_lock(paths: ProjectPaths) -> Iterator[None]:
    with _file_lock(paths.locks_dir / "events.lock"):
        yield


@contextlib.contextmanager
def run_lock(paths: ProjectPaths, run_id: str) -> Iterator[None]:
    validate_run_id(run_id)
    with _file_lock(paths.locks_dir / f"{run_id}.lock"):
        yield


def append_run_event(paths: ProjectPaths, event: dict[str, Any]) -> dict[str, Any]:
    record = dict(event)
    record.setdefault("event_id", uuid.uuid4().hex)
    record.setdefault("at", utc_now())
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    paths.registry_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.registry_root, 0o700)
    with event_lock(paths):
        fd = os.open(paths.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
    return record


def _require_text(value: Any, field: str, *, allow_newlines: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"manifest {field} must be a non-empty string")
    if "\x00" in value or (not allow_newlines and ("\n" in value or "\r" in value)):
        raise ValueError(f"manifest {field} contains invalid control characters")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field)


def _positive_int(value: Any, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"manifest {field} must be a positive integer")
    return value


def process_title_privacy_mode(manifest: dict[str, Any]) -> str | None:
    value = manifest.get("process_title_privacy")
    if value is None:
        return None
    if value != _PROCESS_TITLE_PRIVACY_MANIFEST:
        raise ValueError(
            "manifest process_title_privacy must be exactly {'mode': 'required'}"
        )
    return PROCESS_TITLE_PRIVACY_MODE


def validate_current_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    schema = manifest.get("schema_version")
    if schema not in {PREVIOUS_MANIFEST_SCHEMA, CURRENT_MANIFEST_SCHEMA}:
        raise ValueError("unsupported current manifest schema")
    forbidden = sorted(_FORBIDDEN_CORE_MANIFEST_FIELDS.intersection(manifest))
    if forbidden:
        raise ValueError(
            f"core manifest contains deferred fields: {', '.join(forbidden)}"
        )
    process_title_privacy_mode(manifest)
    manifest["workload_class"] = normalize_workload_class(
        manifest.get("workload_class", "standard")
    )
    run_id = validate_current_run_id(_require_text(manifest.get("run_id"), "run_id"))
    for field in (
        "label",
        "task_id",
        "server",
        "ssh",
        "ssh_profile",
        "remote_workdir",
        "project_python",
        "command_path",
        "command_sha256",
        "created_at",
    ):
        _require_text(manifest.get(field), field)
    command = _require_text(manifest.get("command"), "command", allow_newlines=True)

    project_root = Path(_require_text(manifest.get("project_root"), "project_root"))
    config_path = Path(_require_text(manifest.get("project_config"), "project_config"))
    registry_root = Path(_require_text(manifest.get("registry_root"), "registry_root"))
    if not project_root.is_absolute() or not config_path.is_absolute() or not registry_root.is_absolute():
        raise ValueError("project and registry paths must be absolute")
    if config_path.parent != project_root:
        raise ValueError("project_config parent must equal project_root")
    if registry_root != project_root / ".remote-runner":
        raise ValueError("registry_root must be derived from project_root")

    if not PurePosixPath(str(manifest["remote_workdir"])).is_absolute():
        raise ValueError("remote_workdir must be an absolute POSIX path")
    if not PurePosixPath(str(manifest["project_python"])).is_absolute():
        raise ValueError("project_python must be an absolute POSIX path")
    _positive_int(manifest.get("configured_cores"), "configured_cores")
    if "minimum_cores" in manifest:
        _positive_int(manifest.get("minimum_cores"), "minimum_cores")
        minimum_cores = int(manifest["minimum_cores"])
        if int(manifest["configured_cores"]) < minimum_cores:
            raise ValueError("manifest selected server does not satisfy minimum_cores")
    _positive_int(
        manifest.get("assigned_cores", manifest.get("configured_cores")),
        "assigned_cores",
    )
    _optional_text(manifest.get("expected_revision"), "expected_revision")
    if not isinstance(manifest.get("require_clean_worktree"), bool):
        raise ValueError("manifest require_clean_worktree must be boolean")
    if "output_root" in manifest or "output_relpath" in manifest:
        validate_resolved_output(
            output_root=manifest.get("output_root"),
            output_relpath=manifest.get("output_relpath"),
            output_path=manifest.get("output_path"),
        )
    else:
        _optional_text(manifest.get("output_path"), "output_path")
    if not isinstance(manifest.get("output_metadata"), dict):
        raise ValueError("manifest output_metadata must be a mapping")
    if manifest["command_path"] != "command.sh":
        raise ValueError("current command_path must be command.sh")
    expected_hash = sha256_bytes(command.encode("utf-8"))
    if manifest["command_sha256"] != expected_hash:
        raise ValueError("manifest command digest does not match command")
    if not str(manifest["command_sha256"]).startswith("sha256:"):
        raise ValueError("manifest command_sha256 must be a sha256 value")
    if run_id != manifest["run_id"]:
        raise ValueError("invalid run identity")
    source_revision = manifest.get("source_revision")
    if source_revision is not None:
        if not isinstance(source_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", source_revision):
            raise ValueError("manifest source_revision must be a full Git SHA")
        if manifest.get("expected_revision") != source_revision:
            raise ValueError("manifest expected_revision must equal source_revision")
    prepared_servers = manifest.get("prepared_servers")
    if prepared_servers is not None:
        if (
            not isinstance(prepared_servers, list)
            or not prepared_servers
            or not all(isinstance(item, str) and item for item in prepared_servers)
        ):
            raise ValueError("manifest prepared_servers must be a non-empty string list")
        if manifest["server"] not in prepared_servers:
            raise ValueError("manifest selected server must be prepared")
    submitted_command = manifest.get("submitted_command")
    if submitted_command is not None:
        if not isinstance(submitted_command, str) or not submitted_command.strip():
            raise ValueError("manifest submitted_command must be non-empty shell text")
        if manifest.get("submitted_command_sha256") != sha256_bytes(
            submitted_command.encode("utf-8")
        ):
            raise ValueError("manifest submitted command digest mismatch")
    return manifest


def validate_current_state(state: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    if state.get("state_schema_version") != CURRENT_STATE_SCHEMA:
        raise ValueError("unsupported current state schema")
    state_run_id = validate_current_run_id(_require_text(state.get("run_id"), "run_id"))
    if run_id is not None and state_run_id != run_id:
        raise ValueError("state run_id does not match manifest")
    revision = state.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("state revision must be a non-negative integer")
    if state.get("status") not in AUTHORITATIVE_STATUSES:
        raise ValueError(f"invalid authoritative status: {state.get('status')!r}")
    for field in ("created_at", "updated_at"):
        _require_text(state.get(field), field)
    for field in ("started_at", "finished_at", "error"):
        value = state.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"state {field} must be a string or null")
    exit_code = state.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise ValueError("state exit_code must be an integer or null")
    return state


def load_current_run(
    paths: ProjectPaths,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if registry_kind(paths, run_id) != "current":
        raise ValueError(f"run is not a current schema run: {run_id}")
    manifest = validate_current_manifest(load_yaml(current_manifest_path(paths, run_id)))
    if manifest["run_id"] != run_id:
        raise ValueError("manifest run_id does not match registry path")
    state = validate_current_state(load_yaml(current_state_path(paths, run_id)), run_id)
    return manifest, state


def load_current_state(paths: ProjectPaths, run_id: str) -> dict[str, Any]:
    validate_current_run_id(run_id)
    return validate_current_state(load_yaml(current_state_path(paths, run_id)), run_id)


def purge_current_run(paths: ProjectPaths, run_id: str) -> Path:
    validate_current_run_id(run_id)
    with run_lock(paths, run_id):
        _manifest, state = load_current_run(paths, run_id)
        if state["status"] != "stopped":
            raise ValueError(
                f"run {run_id} is {state['status']}, not eligible for stopped purge"
            )
        source = current_run_dir(paths, run_id)
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"run path is not a private directory: {run_id}")
        _remove_tree(source)
        return source


def stage_terminal_current_run(
    paths: ProjectPaths,
    run_id: str,
    *,
    task_id: str,
    destination: Path,
) -> Path:
    validate_current_run_id(run_id)
    purges_root = (paths.registry_root / "task-purges").resolve()
    resolved_destination = destination.resolve()
    if purges_root not in resolved_destination.parents:
        raise ValueError("run purge destination must be inside task-purges")
    with run_lock(paths, run_id):
        source = current_run_dir(paths, run_id)
        if not source.exists():
            if destination.is_dir() and not destination.is_symlink():
                return destination
            raise FileNotFoundError(f"current run does not exist: {run_id}")
        manifest, state = load_current_run(paths, run_id)
        if manifest["task_id"] != task_id:
            raise ValueError(f"run {run_id} belongs to another task")
        if state["status"] not in TERMINAL_STATUSES:
            raise ValueError(
                f"run {run_id} is {state['status']}, not terminal for task purge"
            )
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"run path is not a private directory: {run_id}")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        if destination.exists():
            raise FileExistsError(f"run purge destination already exists: {run_id}")
        os.rename(source, destination)
        _fsync_directory(paths.runs_dir)
        _fsync_directory(destination.parent)
        return destination


def stage_failed_current_run(
    paths: ProjectPaths,
    run_id: str,
    *,
    task_id: str,
) -> Path:
    validated = validate_current_run_id(run_id)
    destination = (
        paths.registry_root
        / "run-purges"
        / validated
        / "records"
        / "execution"
    )
    expected_root = (paths.registry_root / "run-purges" / validated).resolve()
    if expected_root not in destination.resolve().parents:
        raise ValueError("failed run staging destination escaped run-purges")
    tombstone_path = paths.registry_root / "run-tombstones" / f"{validated}.yaml"
    if tombstone_path.is_symlink() or not tombstone_path.is_file():
        raise ValueError(f"run {validated} has no active run purge")
    tombstone = load_yaml(tombstone_path)
    if (
        tombstone.get("run_id") != validated
        or tombstone.get("status") != "purging"
    ):
        raise ValueError(f"run {validated} purge tombstone does not match execution")
    with run_lock(paths, validated):
        source = current_run_dir(paths, validated)
        if not source.exists():
            if destination.is_dir() and not destination.is_symlink():
                return destination
            raise FileNotFoundError(f"current run does not exist: {validated}")
        manifest, state = load_current_run(paths, validated)
        if manifest["task_id"] != task_id:
            raise ValueError(f"run {validated} belongs to another task")
        if state["status"] != "failed":
            raise ValueError(
                f"run {validated} is {state['status']}, not failed for run purge"
            )
        if source.is_symlink() or not source.is_dir():
            raise ValueError(f"run path is not a private directory: {validated}")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination.parent, 0o700)
        if destination.exists():
            raise FileExistsError(
                f"run purge execution destination already exists: {validated}"
            )
        os.rename(source, destination)
        _fsync_directory(paths.runs_dir)
        _fsync_directory(destination.parent)
        return destination


def compact_run_events(paths: ProjectPaths, run_ids: set[str]) -> dict[str, int]:
    validated = {validate_current_run_id(run_id) for run_id in run_ids}
    if not validated or not paths.events_path.is_file():
        return {"removed": 0, "preserved": 0}
    with event_lock(paths):
        lines = paths.events_path.read_bytes().splitlines(keepends=True)
        kept: list[bytes] = []
        removed = 0
        needles = [run_id.encode("utf-8") for run_id in validated]
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                if any(needle in line for needle in needles):
                    raise ValueError(
                        "cannot safely compact malformed run event containing a purged run id"
                    ) from exc
                kept.append(line)
                continue
            if isinstance(record, dict) and record.get("run_id") in validated:
                removed += 1
            else:
                kept.append(line)

        fd, raw_tmp = tempfile.mkstemp(
            prefix=f".{paths.events_path.name}.",
            dir=paths.events_path.parent,
        )
        temporary = Path(raw_tmp)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as handle:
                for line in kept:
                    handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, paths.events_path)
            _fsync_directory(paths.events_path.parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        return {"removed": removed, "preserved": len(kept)}


def can_transition(old: str, new: str) -> bool:
    return old == new or new in _FORWARD_STATUSES.get(old, set())


def update_current_state(
    paths: ProjectPaths,
    run_id: str,
    expected_revision: int,
    changes: dict[str, Any],
    *,
    action: str = "state_transition",
    lock_held: bool = False,
) -> dict[str, Any]:
    manager = contextlib.nullcontext() if lock_held else run_lock(paths, run_id)
    with manager:
        manifest, state = load_current_run(paths, run_id)
        before = int(state["revision"])
        if before != expected_revision:
            raise RuntimeError(
                f"state revision conflict for {run_id}: expected {expected_revision}, found {before}"
            )
        protected = {"state_schema_version", "run_id", "revision"}
        if protected.intersection(changes):
            raise ValueError("state identity and revision fields cannot be changed directly")
        new_status = str(changes.get("status", state["status"]))
        if not can_transition(str(state["status"]), new_status):
            raise ValueError(f"illegal state transition {state['status']} -> {new_status}")
        updated = dict(state)
        updated.update(changes)
        updated["revision"] = before + 1
        updated["updated_at"] = utc_now()
        validate_current_state(updated, run_id)
        if state["status"] not in TERMINAL_STATUSES and updated["status"] in TERMINAL_STATUSES and is_configured(paths.registry_root):
            enqueue_terminal_output(
                paths.registry_root,
                manifest,
                state_revision=int(updated["revision"]),
                authoritative_status=str(updated["status"]),
                terminal_at=str(updated.get("finished_at") or updated["updated_at"]),
            )
        write_yaml(current_state_path(paths, run_id), updated)
        append_run_event(
            paths,
            {
                "action": action,
                "run_id": run_id,
                "before_revision": before,
                "after_revision": updated["revision"],
                "status": updated["status"],
            },
        )
        return updated


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    if sys.platform == "linux":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
            if result == 0:
                return
            error = ctypes.get_errno()
            if error not in {22, 38}:  # EINVAL, ENOSYS
                raise OSError(error, os.strerror(error), destination)
    if sys.platform == "darwin":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is not None:
            result = renamex_np(os.fsencode(source), os.fsencode(destination), 0x00000004)
            if result == 0:
                return
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), destination)
    raise OSError("atomic no-replace directory rename is unavailable on this platform")


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for root, directories, files in os.walk(path, topdown=False):
        for filename in files:
            Path(root, filename).unlink()
        for dirname in directories:
            Path(root, dirname).rmdir()
    path.rmdir()


def commit_current_run(
    paths: ProjectPaths,
    manifest: dict[str, Any],
    state: dict[str, Any],
    command_bytes: bytes,
) -> Path:
    manifest = validate_current_manifest(manifest)
    run_id = str(manifest["run_id"])
    validate_current_state(state, run_id)
    try:
        command = command_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("final command must be valid UTF-8") from exc
    if command != manifest["command"] or sha256_bytes(command_bytes) != manifest["command_sha256"]:
        raise ValueError("frozen command bytes do not match manifest")

    paths.runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.runs_dir, 0o700)
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{run_id}.", dir=paths.runs_dir))
    try:
        os.chmod(temp_dir, 0o700)
        _write_bytes(temp_dir / "manifest.yaml", _yaml_bytes(manifest))
        _write_bytes(temp_dir / "state.yaml", _yaml_bytes(state))
        _write_bytes(temp_dir / "command.sh", command_bytes)
        _fsync_directory(temp_dir)
        destination = paths.runs_dir / run_id
        _rename_directory_no_replace(temp_dir, destination)
        _fsync_directory(paths.runs_dir)
        return destination / "manifest.yaml"
    except Exception:
        _remove_tree(temp_dir)
        raise


def register_current_run(
    paths: ProjectPaths,
    manifest: dict[str, Any],
    state: dict[str, Any],
    command_bytes: bytes,
) -> Path:
    run_id = str(manifest.get("run_id", ""))
    validate_current_run_id(run_id)
    with registry_lock(paths):
        tombstone = paths.registry_root / "run-tombstones" / f"{run_id}.yaml"
        if tombstone.exists() or tombstone.is_symlink():
            raise FileExistsError(f"run id has been purged and cannot be reused: {run_id}")
        kind = registry_kind(paths, run_id)
        if kind is not None:
            raise FileExistsError(f"run id already exists as {kind}: {run_id}")
        manifest_path = commit_current_run(paths, manifest, state, command_bytes)
        append_run_event(
            paths,
            {
                "action": "registered",
                "run_id": run_id,
                "schema_version": CURRENT_MANIFEST_SCHEMA,
                "revision": state["revision"],
                "status": state["status"],
                "command_sha256": manifest["command_sha256"],
            },
        )
        return manifest_path
