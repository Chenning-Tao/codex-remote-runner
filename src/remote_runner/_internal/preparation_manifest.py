from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from .config import ManagedProjectConfig
from .execution_registry import sha256_bytes
from .output_paths import normalize_output_root
from .source import PreparationResult, resolve_clean_head, runner_ref


PREPARATION_MANIFEST_SCHEMA = 3
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PREPARED_SERVER_FIELDS = {
    "name",
    "ssh",
    "ssh_profile",
    "configured_cores",
    "priority",
    "bare_repo",
    "worktree_root",
    "python",
    "output_root",
    "test_slots",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    preimage = dict(payload)
    preimage.pop("manifest_digest", None)
    return sha256_bytes(_canonical_json(preimage).encode("utf-8"))


def _file_digest(path: Path) -> str:
    return sha256_bytes(path.expanduser().resolve(strict=True).read_bytes())


def build_preparation_manifest(
    *,
    config: ManagedProjectConfig,
    server_registry_path: Path,
    preparation: PreparationResult,
    prepared_servers: list[dict[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": PREPARATION_MANIFEST_SCHEMA,
        "project_id": config.project_id,
        "revision": preparation.revision,
        "ref": preparation.ref,
        "project_config_sha256": _file_digest(config.path),
        "server_registry_sha256": _file_digest(server_registry_path),
        "prepared_servers": [
            {**server, "test_slots": server.get("test_slots", 0)}
            for server in prepared_servers
        ],
        "preparation_failures": [item.__dict__ for item in preparation.failures],
    }
    validate_preparation_manifest_shape(payload)
    payload["manifest_digest"] = _manifest_digest(payload)
    return payload


def validate_preparation_manifest_shape(payload: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version",
        "project_id",
        "revision",
        "ref",
        "project_config_sha256",
        "server_registry_sha256",
        "prepared_servers",
        "preparation_failures",
    }
    if "manifest_digest" in payload:
        expected_fields.add("manifest_digest")
    if set(payload) != expected_fields:
        raise ValueError("preparation manifest fields mismatch")
    schema = payload["schema_version"]
    if schema != PREPARATION_MANIFEST_SCHEMA:
        raise ValueError("unsupported preparation manifest schema")
    project_id = payload["project_id"]
    if not isinstance(project_id, str) or not project_id:
        raise ValueError("preparation manifest project_id is invalid")
    revision = payload["revision"]
    if not isinstance(revision, str) or FULL_SHA_RE.fullmatch(revision) is None:
        raise ValueError("preparation manifest revision is invalid")
    if payload["ref"] != runner_ref(project_id, revision):
        raise ValueError("preparation manifest ref mismatch")
    for field in ("project_config_sha256", "server_registry_sha256"):
        value = payload[field]
        if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ValueError(f"preparation manifest {field} is invalid")

    servers = payload["prepared_servers"]
    if not isinstance(servers, list) or not servers:
        raise ValueError("preparation manifest requires prepared servers")
    names: set[str] = set()
    for server in servers:
        if not isinstance(server, dict) or set(server) != PREPARED_SERVER_FIELDS:
            raise ValueError("preparation manifest prepared server fields mismatch")
        for field in (
            "name",
            "ssh",
            "ssh_profile",
            "bare_repo",
            "worktree_root",
            "python",
        ):
            if not isinstance(server[field], str) or not server[field]:
                raise ValueError(f"preparation manifest prepared server {field} is invalid")
        if server["name"] in names:
            raise ValueError("preparation manifest contains duplicate prepared servers")
        names.add(server["name"])
        cores = server["configured_cores"]
        priority = server["priority"]
        if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
            raise ValueError("preparation manifest configured_cores is invalid")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError("preparation manifest priority is invalid")
        normalize_output_root(
            server["output_root"],
            "preparation manifest prepared server output_root",
        )
        test_slots = server["test_slots"]
        if (
            isinstance(test_slots, bool)
            or not isinstance(test_slots, int)
            or test_slots < 0
        ):
            raise ValueError("preparation manifest test_slots is invalid")

    failures = payload["preparation_failures"]
    if not isinstance(failures, list) or any(
        not isinstance(item, dict)
        or set(item) != {"name", "error"}
        or not isinstance(item["name"], str)
        or not isinstance(item["error"], str)
        for item in failures
    ):
        raise ValueError("preparation manifest failures are invalid")


def load_preparation_manifest(
    path: Path,
    *,
    config: ManagedProjectConfig,
    server_registry_path: Path,
    source_repo: Path,
) -> dict[str, Any]:
    payload = json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("preparation manifest must be an object")
    validate_preparation_manifest_shape(payload)
    if payload.get("manifest_digest") != _manifest_digest(payload):
        raise ValueError("preparation manifest digest mismatch")
    if payload["project_id"] != config.project_id:
        raise ValueError("preparation manifest project mismatch")
    if payload["project_config_sha256"] != _file_digest(config.path):
        raise ValueError("preparation manifest project config changed")
    if payload["server_registry_sha256"] != _file_digest(server_registry_path):
        raise ValueError("preparation manifest server registry changed")
    if resolve_clean_head(source_repo) != payload["revision"]:
        raise ValueError("preparation manifest source revision mismatch")
    return payload


def write_preparation_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_text = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_text)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
