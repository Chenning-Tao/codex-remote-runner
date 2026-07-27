from __future__ import annotations

import base64
import fcntl
import hashlib
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
TAG_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESULT_INTENTS = {"candidate", "supporting", "excluded", "unclassified"}
MAX_EXPERIMENT_MANIFEST_BYTES = 1024 * 1024
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


def _relative_path(value: Any, field: str) -> str:
    text = _text(value, field)
    path = PurePosixPath(text)
    if path.is_absolute() or str(path) != text or ".." in path.parts or text == ".":
        raise ValueError(f"{field} must be a normalized relative POSIX path")
    return text


def _experiment_binding(value: Any, run_id: str, revision: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("experiment_binding must be an object")
    binding = dict(value)
    if binding.get("kind") != "run_binding" or binding.get("schema_version") != 1:
        raise ValueError("unsupported experiment_binding contract")
    if binding.get("run_id") != run_id or binding.get("source_revision") != revision:
        raise ValueError("experiment_binding identity mismatch")
    expects = binding.get("expects_result_manifest")
    if not isinstance(expects, bool):
        raise ValueError("experiment_binding expects_result_manifest must be boolean")
    relpath = binding.get("result_manifest_relpath")
    if expects:
        binding["result_manifest_relpath"] = _relative_path(
            relpath,
            "experiment_binding.result_manifest_relpath",
        )
    elif relpath is not None:
        binding["result_manifest_relpath"] = _relative_path(
            relpath,
            "experiment_binding.result_manifest_relpath",
        )
    return binding


def _result_tags(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("result_tags must be a mapping with at most 32 entries")
    tags: dict[str, str] = {}
    for key, raw_value in value.items():
        if not isinstance(key, str) or TAG_KEY_RE.fullmatch(key) is None:
            raise ValueError("result_tags contains an invalid key")
        tag_value = _text(raw_value, f"result_tags[{key!r}]")
        if len(tag_value) > 256:
            raise ValueError("result_tags values must be at most 256 characters")
        tags[key] = tag_value
    return dict(sorted(tags.items()))


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
    for field in ("revision", "task_id", "label", "succeeded_at"):
        _text(payload.get(field), field)
    metadata = payload.get("output_metadata")
    if not isinstance(metadata, dict):
        raise ValueError("output_metadata must be a mapping")
    result_intent = payload.get("result_intent", "unclassified")
    if result_intent not in RESULT_INTENTS:
        raise ValueError("unsupported result_intent")
    payload["result_intent"] = result_intent
    payload["result_tags"] = _result_tags(payload.get("result_tags", {}))
    payload["experiment_binding"] = _experiment_binding(
        payload.get("experiment_binding"),
        run_id,
        str(payload["revision"]),
    )
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


def _regular_file_under(root: Path, relative_path: str, field: str) -> Path:
    relative = PurePosixPath(_relative_path(relative_path, field))
    current = root
    if current.is_symlink() or not current.is_dir():
        raise ValueError(f"{field} root is not a regular directory")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{field} traverses a symlink")
    if not current.is_file():
        raise ValueError(f"{field} is not a regular file")
    return current


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"experiment result contains invalid JSON constant: {value}")


def _verified_experiment_result(
    payload: dict[str, Any],
    *,
    archived_run_path: Path,
    artifacts_root: Path,
    source_kind: str,
) -> dict[str, Any] | None:
    binding = payload.get("experiment_binding")
    if not isinstance(binding, dict) or binding.get("expects_result_manifest") is not True:
        return None
    if source_kind != "directory":
        raise ValueError("a result-producing experiment run must synchronize a directory")
    manifest_path = _regular_file_under(
        archived_run_path,
        str(binding["result_manifest_relpath"]),
        "experiment result manifest",
    )
    size = manifest_path.stat().st_size
    if size > MAX_EXPERIMENT_MANIFEST_BYTES:
        raise ValueError("experiment result manifest exceeds the size limit")
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"experiment result manifest is invalid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("experiment result manifest must be a JSON object")
    if manifest.get("kind") != "experiment_result" or manifest.get("schema_version") != 1:
        raise ValueError("unsupported experiment result manifest contract")
    if manifest.get("emitter_run_id") != payload["run_id"]:
        raise ValueError("experiment result emitter_run_id mismatch")
    results = manifest.get("results")
    if not isinstance(results, list):
        raise ValueError("experiment result manifest results must be a list")
    verified_artifacts = 0
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("artifacts"), list):
            raise ValueError("experiment result artifacts must be a list")
        for artifact in result["artifacts"]:
            if not isinstance(artifact, dict):
                raise ValueError("experiment result artifact must be an object")
            artifact_run_id = artifact.get("run_id")
            if not isinstance(artifact_run_id, str) or RUN_ID_RE.fullmatch(artifact_run_id) is None:
                raise ValueError("experiment result artifact run_id is invalid")
            artifact_root = (
                archived_run_path
                if artifact_run_id == payload["run_id"]
                else artifacts_root / artifact_run_id
            )
            artifact_path = _regular_file_under(
                artifact_root,
                str(artifact.get("relative_path")),
                "experiment result artifact",
            )
            if _sha256_file(artifact_path) != artifact.get("sha256"):
                raise ValueError("experiment result artifact digest mismatch")
            verified_artifacts += 1
    canonical = json.dumps(
        manifest,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "manifest": manifest,
        "canonical_sha256": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
        "artifact_count": verified_artifacts,
    }


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
            experiment_result = _verified_experiment_result(
                payload,
                archived_run_path=target,
                artifacts_root=artifacts_root,
                source_kind=source_kind,
            )
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
            experiment_result = _verified_experiment_result(
                payload,
                archived_run_path=stage,
                artifacts_root=artifacts_root,
                source_kind=source_kind,
            )
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
            "result_intent": payload["result_intent"],
            "result_tags": payload["result_tags"],
            "succeeded_at": payload["succeeded_at"],
            "archived_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "verification": "rsync_checksum_dry_run",
            "disposition": disposition,
            "source_deletion_performed": False,
        }
        if experiment_result is not None:
            receipt["experiment_result"] = experiment_result
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
