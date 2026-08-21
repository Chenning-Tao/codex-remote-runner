"""Identity, immutable spec digest, and deterministic run ID for derived runs.

A derived validation run is an ordinary durable run that Remote Runner submits on
behalf of exactly one reportable source run. The relation recorded here is the only
authoritative link between the two: the source record is never modified in place.

Both the local client and the controller import this module, and the controller
recomputes every value before it trusts one. Nothing in this module interprets the
project payload a validator produces.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any

from .execution_registry import sha256_bytes, validate_current_run_id
from .output_paths import normalize_absolute_output_path, normalize_output_relpath


DERIVATION_SCHEMA_VERSION = 1
SPEC_DIGEST_SCHEMA_VERSION = 1
RECEIPT_IDENTITY_SCHEMA_VERSION = 1
VALIDATION_KIND = "validation"
VALIDATOR_KEY_MAX_LENGTH = 128

_VALIDATOR_KEY_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$"
)
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SERVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECEIPT_IDENTITY_FIELDS = (
    "run_id",
    "source_server",
    "source_path",
    "source_kind",
    "target_path",
    "revision",
    "authoritative_status",
    "terminal_at",
    "archived_at",
    "verification",
    "disposition",
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _text(value: Any, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"derivation {field} must be a non-empty string")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"derivation {field} contains invalid control characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"derivation {field} has an invalid value")
    return value


def normalize_validator_key(value: Any) -> str:
    key = _text(value, "validator_key")
    if len(key) > VALIDATOR_KEY_MAX_LENGTH:
        raise ValueError(
            f"validator key must be at most {VALIDATOR_KEY_MAX_LENGTH} characters"
        )
    if _VALIDATOR_KEY_RE.fullmatch(key) is None:
        raise ValueError(
            "validator key must be lowercase alphanumeric segments separated by "
            "'/', '.', '_', or '-'"
        )
    return key


def normalize_artifact_path(value: Any, field: str) -> str:
    """Normalize one absolute artifact path and refuse traversal components."""
    text = normalize_absolute_output_path(value, field)
    if any(part in {".", ".."} for part in PurePosixPath(text).parts):
        raise ValueError(f"{field} cannot contain dot or parent traversal components")
    return text


def normalize_result_relpath(value: Any) -> str:
    return normalize_output_relpath(value, "--result-relpath")


def derived_run_id(
    *,
    project_id: str,
    source_run_id: str,
    validator_key: str,
) -> str:
    """Derive the only run ID a (project, source, validator key) triple can own."""
    identity = [
        _text(project_id, "project_id"),
        VALIDATION_KIND,
        validate_current_run_id(_text(source_run_id, "source_run_id")),
        normalize_validator_key(validator_key),
    ]
    digest = hashlib.sha256(canonical_bytes(identity)).hexdigest()
    return validate_current_run_id("rr-" + digest[:16])


def receipt_identity(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ValueError("output-sync receipt must be a mapping")
    identity: dict[str, Any] = {"schema_version": RECEIPT_IDENTITY_SCHEMA_VERSION}
    for field in _RECEIPT_IDENTITY_FIELDS:
        value = receipt.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"output-sync receipt {field} must be a non-empty string")
        identity[field] = value
    return identity


def receipt_identity_sha256(receipt: Any) -> str:
    return sha256_bytes(canonical_bytes(receipt_identity(receipt)))


def build_relation(
    *,
    source_run_id: Any,
    source_revision: Any,
    source_server: Any,
    target_server: Any,
    target_path: Any,
    receipt_sha256: Any,
    validator_key: Any,
    result_relpath: Any,
) -> dict[str, Any]:
    """Build the relation identity; `spec_sha256` is attached separately.

    Every argument is validated here, so callers may pass values straight from a
    stored record or a submitted payload without pre-checking them.
    """
    return {
        "schema_version": DERIVATION_SCHEMA_VERSION,
        "kind": VALIDATION_KIND,
        "source_run_id": validate_current_run_id(_text(source_run_id, "source_run_id")),
        "source_revision": _text(source_revision, "source_revision", _REVISION_RE),
        "source_server": _text(source_server, "source_server", _SERVER_RE),
        "source_artifact": {
            "target_server": _text(target_server, "target_server", _SERVER_RE),
            "target_path": normalize_artifact_path(
                target_path, "derivation target_path"
            ),
            "receipt_sha256": _text(receipt_sha256, "receipt_sha256", _DIGEST_RE),
        },
        "validator_key": normalize_validator_key(validator_key),
        "result_relpath": normalize_result_relpath(result_relpath),
    }


def relation_identity(relation: dict[str, Any]) -> dict[str, Any]:
    """Return the relation without its own digest, for comparison and digesting."""
    return {key: value for key, value in relation.items() if key != "spec_sha256"}


def spec_digest(
    relation: dict[str, Any],
    *,
    label: str,
    task_id: str,
    submitted_command_sha256: str,
    minimum_cores: int,
    requested_cores: int | None,
    workload_class: str,
    output_relpath: str,
    privacy: str | None,
    eligible_servers: list[str],
) -> str:
    """Digest every immutable input of one derived submission.

    Queue position, created time, and mutable state never take part: the digest must
    stay stable across an exact retry so the submission can be reused rather than
    duplicated.
    """
    spec = {
        "schema_version": SPEC_DIGEST_SCHEMA_VERSION,
        "identity": relation_identity(relation),
        "label": _text(label, "label"),
        "task_id": _text(task_id, "task_id"),
        "submitted_command_sha256": _text(
            submitted_command_sha256, "submitted_command_sha256", _DIGEST_RE
        ),
        "minimum_cores": int(minimum_cores),
        "requested_cores": None if requested_cores is None else int(requested_cores),
        "workload_class": _text(workload_class, "workload_class"),
        "output_relpath": normalize_output_relpath(output_relpath),
        "privacy": None if privacy is None else _text(privacy, "privacy"),
        "eligible_servers": sorted(
            _text(name, "eligible_server", _SERVER_RE) for name in eligible_servers
        ),
    }
    return sha256_bytes(canonical_bytes(spec))


def validate_relation(value: Any) -> dict[str, Any]:
    """Validate a stored or submitted relation payload and return a normalized copy."""
    if not isinstance(value, dict):
        raise ValueError("derivation must be a mapping")
    if value.get("schema_version") != DERIVATION_SCHEMA_VERSION:
        raise ValueError("unsupported derivation schema")
    if value.get("kind") != VALIDATION_KIND:
        raise ValueError("unsupported derivation kind")
    artifact = value.get("source_artifact")
    if not isinstance(artifact, dict):
        raise ValueError("derivation source_artifact must be a mapping")
    relation = build_relation(
        source_run_id=value.get("source_run_id"),
        source_revision=value.get("source_revision"),
        source_server=value.get("source_server"),
        target_server=artifact.get("target_server"),
        target_path=artifact.get("target_path"),
        receipt_sha256=artifact.get("receipt_sha256"),
        validator_key=value.get("validator_key"),
        result_relpath=value.get("result_relpath"),
    )
    unknown = sorted(set(value) - set(relation) - {"spec_sha256"})
    if unknown:
        raise ValueError(f"derivation has unsupported fields: {', '.join(unknown)}")
    unknown_artifact = sorted(set(artifact) - set(relation["source_artifact"]))
    if unknown_artifact:
        raise ValueError(
            "derivation source_artifact has unsupported fields: "
            + ", ".join(unknown_artifact)
        )
    relation["spec_sha256"] = _text(value.get("spec_sha256"), "spec_sha256", _DIGEST_RE)
    return relation


def artifact_result_path(relation: dict[str, Any], target_path: str) -> str:
    """Resolve one result path below an exact validator artifact root."""
    root = PurePosixPath(normalize_artifact_path(target_path, "validator target_path"))
    resolved = root / PurePosixPath(relation["result_relpath"])
    try:
        resolved.relative_to(root)
    except ValueError as exc:  # pragma: no cover - normalization already rejects this
        raise ValueError("result path escapes the validator artifact root") from exc
    return str(resolved)
