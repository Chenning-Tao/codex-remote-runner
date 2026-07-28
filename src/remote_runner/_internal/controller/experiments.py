from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import secrets
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..experiment_contracts import (
    EXPERIMENT_SCHEMA_VERSION,
    canonical_json_bytes,
    contract_digest,
    normalize_acceptance_request,
    normalize_experiment_plan,
    normalize_experiment_query,
    normalize_experiment_result,
    normalize_run_binding,
)
from ..execution_registry import load_current_run, project_paths
from ..output_sync import list_completed_syncs
from .registry import ControllerPaths, list_jobs


REGISTRY_SCHEMA_VERSION = 1
PROJECTOR_VERSION = 1
MAX_QUERY_RESPONSE_BYTES = 512 * 1024
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^rr-[0-9a-f]{16}$")
STUDY_QUERY_FIELDS = frozenset(
    {
        "study_id",
        "canonical_key",
        "display_name",
        "description",
        "active_design_revision_id",
        "previous_design_revision_id",
        "plan_digest",
        "dimensions",
        "metrics",
        "presentation",
        "impact",
        "status_counts",
        "point_count",
        "event_cursor",
    }
)
POINT_QUERY_FIELDS = frozenset(
    {
        "plan_order",
        "point_id",
        "point_revision_id",
        "canonical_key",
        "display_name",
        "dimensions",
        "status",
        "metrics",
        "accepted_acceptance_id",
        "accepted_result_id",
        "observation_count",
        "candidate_count",
        "has_stale_history",
        "stale_reason",
        "setting_digest",
        "point_revision_digest",
        "requirements",
        "runs",
        "metric_catalog",
        "result_history",
        "artifacts",
        "is_active",
        "revision_event_sequence",
    }
)
DEFAULT_POINT_LIST_FIELDS = (
    "point_id",
    "point_revision_id",
    "canonical_key",
    "display_name",
    "dimensions",
    "status",
    "metrics",
    "accepted_acceptance_id",
    "accepted_result_id",
    "observation_count",
    "candidate_count",
    "has_stale_history",
    "stale_reason",
    "setting_digest",
    "point_revision_digest",
    "runs",
)
DEFAULT_RERUN_FIELDS = (
    "point_id",
    "point_revision_id",
    "canonical_key",
    "display_name",
    "dimensions",
    "status",
    "stale_reason",
    "setting_digest",
    "point_revision_digest",
)


@dataclass(frozen=True)
class ExperimentPaths:
    root: Path
    journal_dir: Path
    database_path: Path
    backups_dir: Path
    locks_dir: Path


def experiment_paths(paths: ControllerPaths) -> ExperimentPaths:
    root = paths.registry_root / "experiments"
    return ExperimentPaths(
        root=root,
        journal_dir=root / "journal",
        database_path=root / "registry.sqlite3",
        backups_dir=root / "backups",
        locks_dir=root / "locks",
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _experiment_lock(paths: ControllerPaths) -> Iterator[None]:
    target = experiment_paths(paths)
    _private_dir(target.locks_dir)
    descriptor = os.open(
        target.locks_dir / "registry.lock", os.O_RDWR | os.O_CREAT, 0o600
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projection_inputs (
  source_id TEXT PRIMARY KEY,
  source_digest TEXT NOT NULL,
  sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_events (
  sequence INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  event_digest TEXT NOT NULL,
  occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS studies (
  study_id TEXT PRIMARY KEY,
  canonical_key TEXT NOT NULL UNIQUE,
  created_event_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS study_names (
  study_id TEXT NOT NULL REFERENCES studies(study_id),
  sequence INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  description TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  PRIMARY KEY (study_id, sequence)
);
CREATE TABLE IF NOT EXISTS study_aliases (
  study_id TEXT NOT NULL REFERENCES studies(study_id),
  alias TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  PRIMARY KEY (study_id, alias)
);
CREATE TABLE IF NOT EXISTS study_heads (
  study_id TEXT PRIMARY KEY REFERENCES studies(study_id),
  active_design_revision_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS design_revisions (
  design_revision_id TEXT PRIMARY KEY,
  study_id TEXT NOT NULL REFERENCES studies(study_id),
  plan_digest TEXT NOT NULL,
  plan_json TEXT NOT NULL,
  impact_json TEXT NOT NULL,
  published_event_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS points (
  point_id TEXT PRIMARY KEY,
  study_id TEXT NOT NULL REFERENCES studies(study_id),
  canonical_key TEXT NOT NULL,
  created_event_id TEXT NOT NULL,
  UNIQUE (study_id, canonical_key)
);
CREATE TABLE IF NOT EXISTS point_names (
  point_id TEXT NOT NULL REFERENCES points(point_id),
  sequence INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  PRIMARY KEY (point_id, sequence)
);
CREATE TABLE IF NOT EXISTS point_aliases (
  point_id TEXT NOT NULL REFERENCES points(point_id),
  alias TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  PRIMARY KEY (point_id, alias)
);
CREATE TABLE IF NOT EXISTS point_revisions (
  point_revision_id TEXT PRIMARY KEY,
  point_id TEXT NOT NULL REFERENCES points(point_id),
  point_revision_digest TEXT NOT NULL,
  setting_digest TEXT NOT NULL,
  dimensions_json TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  dependencies_json TEXT NOT NULL,
  requirements_json TEXT NOT NULL,
  created_event_id TEXT NOT NULL,
  UNIQUE (point_id, point_revision_digest)
);
CREATE TABLE IF NOT EXISTS design_points (
  design_revision_id TEXT NOT NULL REFERENCES design_revisions(design_revision_id),
  point_id TEXT NOT NULL REFERENCES points(point_id),
  point_revision_id TEXT NOT NULL REFERENCES point_revisions(point_revision_id),
  plan_order INTEGER NOT NULL,
  PRIMARY KEY (design_revision_id, point_id)
);
CREATE TABLE IF NOT EXISTS run_bindings (
  binding_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  binding_digest TEXT NOT NULL,
  binding_json TEXT NOT NULL,
  observed_event_id TEXT NOT NULL,
  UNIQUE (run_id, binding_id)
);
CREATE TABLE IF NOT EXISTS run_binding_targets (
  binding_id TEXT NOT NULL REFERENCES run_bindings(binding_id),
  point_revision_id TEXT NOT NULL REFERENCES point_revisions(point_revision_id),
  contribution_role TEXT NOT NULL,
  result_group_id TEXT NOT NULL,
  PRIMARY KEY (binding_id, point_revision_id, result_group_id)
);
CREATE TABLE IF NOT EXISTS results (
  result_id TEXT PRIMARY KEY,
  manifest_id TEXT NOT NULL,
  manifest_digest TEXT NOT NULL,
  study_id TEXT NOT NULL REFERENCES studies(study_id),
  point_id TEXT NOT NULL REFERENCES points(point_id),
  point_revision_id TEXT NOT NULL REFERENCES point_revisions(point_revision_id),
  result_group_id TEXT NOT NULL,
  observation_count INTEGER NOT NULL,
  eligible INTEGER NOT NULL,
  ineligibility_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  ingested_event_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS result_metrics (
  result_id TEXT NOT NULL REFERENCES results(result_id),
  metric_key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  numeric_value REAL,
  interval_json TEXT,
  PRIMARY KEY (result_id, metric_key)
);
CREATE TABLE IF NOT EXISTS result_artifacts (
  result_id TEXT NOT NULL REFERENCES results(result_id),
  run_id TEXT NOT NULL,
  role TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  media_type TEXT NOT NULL,
  size INTEGER,
  PRIMARY KEY (result_id, run_id, role, relative_path)
);
CREATE TABLE IF NOT EXISTS result_runs (
  result_id TEXT NOT NULL REFERENCES results(result_id),
  run_id TEXT NOT NULL,
  binding_id TEXT NOT NULL REFERENCES run_bindings(binding_id),
  role TEXT NOT NULL,
  replaces_run_id TEXT,
  PRIMARY KEY (result_id, run_id, binding_id)
);
CREATE TABLE IF NOT EXISTS acceptances (
  acceptance_id TEXT PRIMARY KEY,
  point_revision_id TEXT NOT NULL REFERENCES point_revisions(point_revision_id),
  result_id TEXT NOT NULL REFERENCES results(result_id),
  action TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  policy TEXT NOT NULL,
  supersedes_acceptance_id TEXT,
  event_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accepted_result_heads (
  point_revision_id TEXT PRIMARY KEY REFERENCES point_revisions(point_revision_id),
  acceptance_id TEXT NOT NULL REFERENCES acceptances(acceptance_id),
  result_id TEXT NOT NULL REFERENCES results(result_id)
);
"""


def _initialize_database(database_path: Path, *, epoch: str | None = None) -> None:
    _private_dir(database_path.parent)
    with _connect(database_path) as connection:
        connection.executescript(SCHEMA)
        values = {
            "schema_version": str(REGISTRY_SCHEMA_VERSION),
            "projector_version": str(PROJECTOR_VERSION),
            "registry_epoch": epoch or secrets.token_hex(16),
        }
        for key, value in values.items():
            connection.execute(
                "INSERT OR IGNORE INTO registry_meta(key, value) VALUES (?, ?)",
                (key, value),
            )
    os.chmod(database_path, 0o600)


def ensure_registry(paths: ControllerPaths) -> ExperimentPaths:
    target = experiment_paths(paths)
    for directory in (
        target.root,
        target.journal_dir,
        target.backups_dir,
        target.locks_dir,
    ):
        _private_dir(directory)
    if not target.database_path.is_file():
        _initialize_database(target.database_path)
    return target


def _journal_events(target: ExperimentPaths) -> list[dict[str, Any]]:
    if not target.journal_dir.is_dir():
        return []
    events: list[dict[str, Any]] = []
    for path in sorted(target.journal_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"experiment journal event must be a regular file: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"experiment journal event is not an object: {path}")
        events.append(value)
    previous_digest: str | None = None
    for expected, event in enumerate(events, start=1):
        if event.get("sequence") != expected:
            raise ValueError("experiment journal sequence is not contiguous")
        if event.get("schema_version") != EXPERIMENT_SCHEMA_VERSION:
            raise ValueError("unsupported experiment journal schema_version")
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            raise ValueError("experiment journal event_id is invalid")
        expected_name = f"{expected:020d}-{event_id}.json"
        if not (target.journal_dir / expected_name).is_file():
            raise ValueError("experiment journal filename does not match its identity")
        if event.get("project_id") != target.root.parent.parent.name:
            raise ValueError("experiment journal project identity mismatch")
        if event.get("previous_event_digest") != previous_digest:
            raise ValueError("experiment journal digest chain is invalid")
        computed = contract_digest(_event_without_digest(event))
        if event.get("event_digest") != computed:
            raise ValueError(f"experiment journal event digest mismatch: {event_id}")
        previous_digest = computed
    return events


def _event_without_digest(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "event_digest"}


def _append_event_locked(
    paths: ControllerPaths,
    *,
    event_type: str,
    payload: dict[str, Any],
    request_id: str,
    request_digest: str,
) -> tuple[dict[str, Any], bool]:
    target = ensure_registry(paths)
    events = _journal_events(target)
    for event in events:
        if event.get("request_id") != request_id:
            continue
        if event.get("request_digest") != request_digest:
            raise RuntimeError(
                "experiment request id was reused with different content"
            )
        return event, False
    previous_digest = None if not events else events[-1].get("event_digest")
    sequence = len(events) + 1
    event = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "event_id": _new_id("experiment-event"),
        "sequence": sequence,
        "project_id": paths.project_id,
        "event_type": event_type,
        "occurred_at": _now(),
        "previous_event_digest": previous_digest,
        "request_id": request_id,
        "request_digest": request_digest,
        "payload": payload,
    }
    event["event_digest"] = contract_digest(event)
    destination = target.journal_dir / f"{sequence:020d}-{event['event_id']}.json"
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=".event-", dir=target.journal_dir
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(canonical_json_bytes(event))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        temporary.unlink()
        _fsync_directory(target.journal_dir)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    return event, True


def _latest_name(
    connection: sqlite3.Connection, table: str, identity: str, field: str
) -> sqlite3.Row | None:
    identity_field = "study_id" if table == "study_names" else "point_id"
    return connection.execute(
        f"SELECT * FROM {table} WHERE {identity_field} = ? ORDER BY sequence DESC LIMIT 1",
        (identity,),
    ).fetchone()


def _apply_plan_event(connection: sqlite3.Connection, event: Mapping[str, Any]) -> None:
    payload = dict(event["payload"])
    plan = dict(payload["plan"])
    study = dict(plan["study"])
    sequence = int(event["sequence"])
    study_id = str(study["study_id"])
    design_revision_id = str(payload["design_revision_id"])
    connection.execute(
        "INSERT OR IGNORE INTO studies(study_id, canonical_key, created_event_id) VALUES (?, ?, ?)",
        (study_id, study["canonical_key"], event["event_id"]),
    )
    current_name = _latest_name(connection, "study_names", study_id, "display_name")
    if (
        current_name is None
        or current_name["display_name"] != study["display_name"]
        or current_name["description"] != study["description"]
    ):
        connection.execute(
            "INSERT INTO study_names(study_id, sequence, display_name, description, metadata_json) VALUES (?, ?, ?, ?, ?)",
            (
                study_id,
                sequence,
                study["display_name"],
                study["description"],
                canonical_json_bytes(study["metadata"]).decode(),
            ),
        )
    for alias in study["aliases"]:
        connection.execute(
            "INSERT OR IGNORE INTO study_aliases(study_id, alias, sequence) VALUES (?, ?, ?)",
            (study_id, alias, sequence),
        )
    connection.execute(
        "INSERT OR IGNORE INTO design_revisions(design_revision_id, study_id, plan_digest, plan_json, impact_json, published_event_id) VALUES (?, ?, ?, ?, ?, ?)",
        (
            design_revision_id,
            study_id,
            plan["plan_digest"],
            canonical_json_bytes(plan).decode(),
            canonical_json_bytes(payload["impact"]).decode(),
            event["event_id"],
        ),
    )
    for plan_order, point in enumerate(plan["points"]):
        point_id = str(point["point_id"])
        point_revision_id = str(point["point_revision_id"])
        connection.execute(
            "INSERT OR IGNORE INTO points(point_id, study_id, canonical_key, created_event_id) VALUES (?, ?, ?, ?)",
            (point_id, study_id, point["canonical_key"], event["event_id"]),
        )
        current_point_name = _latest_name(
            connection, "point_names", point_id, "display_name"
        )
        if (
            current_point_name is None
            or current_point_name["display_name"] != point["display_name"]
        ):
            connection.execute(
                "INSERT INTO point_names(point_id, sequence, display_name, metadata_json) VALUES (?, ?, ?, ?)",
                (
                    point_id,
                    sequence,
                    point["display_name"],
                    canonical_json_bytes(point["metadata"]).decode(),
                ),
            )
        for alias in point["aliases"]:
            connection.execute(
                "INSERT OR IGNORE INTO point_aliases(point_id, alias, sequence) VALUES (?, ?, ?)",
                (point_id, alias, sequence),
            )
        connection.execute(
            """INSERT OR IGNORE INTO point_revisions(
                 point_revision_id, point_id, point_revision_digest, setting_digest,
                 dimensions_json, parameters_json, dependencies_json, requirements_json,
                 created_event_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                point_revision_id,
                point_id,
                point["point_revision_digest"],
                point["setting_digest"],
                canonical_json_bytes(point["dimensions"]).decode(),
                canonical_json_bytes(point["parameters"]).decode(),
                canonical_json_bytes(point["setting_dependencies"]).decode(),
                canonical_json_bytes(point["result_requirements"]).decode(),
                event["event_id"],
            ),
        )
        connection.execute(
            "INSERT OR IGNORE INTO design_points(design_revision_id, point_id, point_revision_id, plan_order) VALUES (?, ?, ?, ?)",
            (design_revision_id, point_id, point_revision_id, plan_order),
        )
    connection.execute(
        "INSERT INTO study_heads(study_id, active_design_revision_id) VALUES (?, ?) ON CONFLICT(study_id) DO UPDATE SET active_design_revision_id = excluded.active_design_revision_id",
        (study_id, design_revision_id),
    )


def _apply_binding_event(
    connection: sqlite3.Connection, event: Mapping[str, Any]
) -> None:
    binding = dict(event["payload"]["binding"])
    connection.execute(
        "INSERT OR IGNORE INTO run_bindings(binding_id, run_id, binding_digest, binding_json, observed_event_id) VALUES (?, ?, ?, ?, ?)",
        (
            binding["binding_id"],
            binding["run_id"],
            binding["binding_digest"],
            canonical_json_bytes(binding).decode(),
            event["event_id"],
        ),
    )
    for target in binding["targets"]:
        connection.execute(
            "INSERT OR IGNORE INTO run_binding_targets(binding_id, point_revision_id, contribution_role, result_group_id) VALUES (?, ?, ?, ?)",
            (
                binding["binding_id"],
                target["point_revision_id"],
                target["contribution_role"],
                target["result_group_id"],
            ),
        )


def _apply_result_event(
    connection: sqlite3.Connection, event: Mapping[str, Any]
) -> None:
    payload = dict(event["payload"])
    manifest = dict(payload["manifest"])
    eligibility = dict(payload["eligibility"])
    for result in manifest["results"]:
        reasons = eligibility.get(result["result_id"], [])
        connection.execute(
            """INSERT OR IGNORE INTO results(
                 result_id, manifest_id, manifest_digest, study_id, point_id,
                 point_revision_id, result_group_id, observation_count, eligible,
                 ineligibility_json, metadata_json, ingested_event_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result["result_id"],
                manifest["manifest_id"],
                manifest["manifest_digest"],
                result["study_id"],
                result["point_id"],
                result["point_revision_id"],
                result["result_group_id"],
                result["evidence"]["observation_count"],
                int(not reasons),
                canonical_json_bytes(reasons).decode(),
                canonical_json_bytes(result["metadata"]).decode(),
                event["event_id"],
            ),
        )
        for metric in result["metrics"]:
            numeric = (
                metric["value"]
                if isinstance(metric["value"], (int, float))
                and not isinstance(metric["value"], bool)
                else None
            )
            connection.execute(
                "INSERT OR IGNORE INTO result_metrics(result_id, metric_key, value_json, numeric_value, interval_json) VALUES (?, ?, ?, ?, ?)",
                (
                    result["result_id"],
                    metric["key"],
                    canonical_json_bytes(metric["value"]).decode(),
                    numeric,
                    None
                    if metric["interval"] is None
                    else canonical_json_bytes(metric["interval"]).decode(),
                ),
            )
        for artifact in result["artifacts"]:
            connection.execute(
                "INSERT OR IGNORE INTO result_artifacts(result_id, run_id, role, relative_path, sha256, media_type, size) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    result["result_id"],
                    artifact["run_id"],
                    artifact["role"],
                    artifact["relative_path"],
                    artifact["sha256"],
                    artifact["media_type"],
                    artifact["size"],
                ),
            )
        for contribution in result["contributions"]:
            connection.execute(
                "INSERT OR IGNORE INTO result_runs(result_id, run_id, binding_id, role, replaces_run_id) VALUES (?, ?, ?, ?, ?)",
                (
                    result["result_id"],
                    contribution["run_id"],
                    contribution["binding_id"],
                    contribution["role"],
                    contribution["replaces_run_id"],
                ),
            )


def _apply_acceptance_event(
    connection: sqlite3.Connection, event: Mapping[str, Any]
) -> None:
    acceptance = dict(event["payload"]["acceptance"])
    connection.execute(
        "INSERT OR IGNORE INTO acceptances(acceptance_id, point_revision_id, result_id, action, actor, reason, policy, supersedes_acceptance_id, event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            acceptance["acceptance_id"],
            acceptance["point_revision_id"],
            acceptance["result_id"],
            acceptance["action"],
            acceptance["actor"],
            acceptance["reason"],
            acceptance["policy"],
            acceptance["supersedes_acceptance_id"],
            event["event_id"],
        ),
    )
    if acceptance["action"] == "revoke":
        connection.execute(
            "DELETE FROM accepted_result_heads WHERE point_revision_id = ?",
            (acceptance["point_revision_id"],),
        )
    elif acceptance["action"] != "reject":
        connection.execute(
            "INSERT INTO accepted_result_heads(point_revision_id, acceptance_id, result_id) VALUES (?, ?, ?) ON CONFLICT(point_revision_id) DO UPDATE SET acceptance_id = excluded.acceptance_id, result_id = excluded.result_id",
            (
                acceptance["point_revision_id"],
                acceptance["acceptance_id"],
                acceptance["result_id"],
            ),
        )


def _apply_event_to_database(database_path: Path, event: Mapping[str, Any]) -> bool:
    event_digest = contract_digest(_event_without_digest(event))
    if event.get("event_digest") != event_digest:
        raise ValueError(
            f"experiment journal event digest mismatch: {event.get('event_id')}"
        )
    with _connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT source_digest FROM projection_inputs WHERE source_id = ?",
            (event["event_id"],),
        ).fetchone()
        if previous is not None:
            if previous["source_digest"] != event_digest:
                raise RuntimeError("experiment projection input digest conflict")
            connection.rollback()
            return False
        event_type = event["event_type"]
        if event_type == "plan_published":
            _apply_plan_event(connection, event)
        elif event_type == "binding_observed":
            _apply_binding_event(connection, event)
        elif event_type == "result_ingested":
            _apply_result_event(connection, event)
        elif event_type == "acceptance_recorded":
            _apply_acceptance_event(connection, event)
        else:
            raise ValueError(f"unsupported experiment journal event type: {event_type}")
        connection.execute(
            "INSERT INTO projection_inputs(source_id, source_digest, sequence) VALUES (?, ?, ?)",
            (event["event_id"], event_digest, event["sequence"]),
        )
        connection.execute(
            "INSERT INTO registry_events(sequence, event_id, event_type, event_digest, occurred_at) VALUES (?, ?, ?, ?, ?)",
            (
                event["sequence"],
                event["event_id"],
                event_type,
                event_digest,
                event["occurred_at"],
            ),
        )
        connection.commit()
    return True


def _apply_event(paths: ControllerPaths, event: Mapping[str, Any]) -> bool:
    target = ensure_registry(paths)
    return _apply_event_to_database(target.database_path, event)


def _catch_up_locked(paths: ControllerPaths) -> int:
    target = ensure_registry(paths)
    applied = 0
    for event in _journal_events(target):
        applied += int(_apply_event_to_database(target.database_path, event))
    return applied


def experiment_purge_blockers(
    paths: ControllerPaths,
    run_ids: Iterable[str],
) -> list[dict[str, Any]]:
    normalized_run_ids = sorted(set(run_ids))
    for run_id in normalized_run_ids:
        if RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError(f"invalid experiment provenance run id: {run_id}")
    target = experiment_paths(paths)
    if not target.journal_dir.is_dir():
        return []

    blockers: list[dict[str, Any]] = []
    with _experiment_lock(paths):
        _catch_up_locked(paths)
        with _connect(target.database_path) as connection:
            for run_id in normalized_run_ids:
                references: dict[str, set[str]] = {}
                rows = connection.execute(
                    """SELECT result_id, 'contribution' AS reference_kind
                       FROM result_runs WHERE run_id = ?
                       UNION
                       SELECT result_id, 'artifact' AS reference_kind
                       FROM result_artifacts WHERE run_id = ?
                       UNION
                       SELECT result_id, 'replaced_run' AS reference_kind
                       FROM result_runs WHERE replaces_run_id = ?""",
                    (run_id, run_id, run_id),
                ).fetchall()
                for row in rows:
                    references.setdefault(str(row["result_id"]), set()).add(
                        str(row["reference_kind"])
                    )
                if not references:
                    continue
                accepted_result_ids = sorted(
                    result_id
                    for result_id in references
                    if connection.execute(
                        "SELECT 1 FROM accepted_result_heads WHERE result_id = ?",
                        (result_id,),
                    ).fetchone()
                    is not None
                )
                blockers.append(
                    {
                        "run_id": run_id,
                        "error": (
                            "run is referenced by immutable experiment results; "
                            "purge requires experiment provenance tombstone support"
                        ),
                        "result_ids": sorted(references),
                        "accepted_result_ids": accepted_result_ids,
                        "reference_kinds": sorted(
                            {kind for kinds in references.values() for kind in kinds}
                        ),
                    }
                )
    return blockers


def rebuild_registry(paths: ControllerPaths) -> dict[str, Any]:
    with _experiment_lock(paths):
        target = ensure_registry(paths)
        events = _journal_events(target)
        temporary = target.root / ".registry.rebuild.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                Path(f"{temporary}{suffix}").unlink()
        _initialize_database(temporary, epoch=secrets.token_hex(16))
        for event in events:
            _apply_event_to_database(temporary, event)
        with contextlib.closing(sqlite3.connect(temporary, timeout=5)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                Path(f"{temporary}{suffix}").unlink()
        with contextlib.closing(_connect(target.database_path)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                Path(f"{target.database_path}{suffix}").unlink()
        with temporary.open("rb") as rebuilt_database:
            os.fsync(rebuilt_database.fileno())
        os.replace(temporary, target.database_path)
        os.chmod(target.database_path, 0o600)
        _fsync_directory(target.root)
        return {
            "rebuilt": True,
            "event_count": len(events),
            "registry_epoch": _registry_meta(target.database_path)["registry_epoch"],
        }


def _registry_meta(database_path: Path) -> dict[str, str]:
    with _connect(database_path) as connection:
        return {
            str(row["key"]): str(row["value"])
            for row in connection.execute("SELECT key, value FROM registry_meta")
        }


def _active_study(
    connection: sqlite3.Connection, plan: Mapping[str, Any]
) -> tuple[sqlite3.Row | None, str | None]:
    study = plan["study"]
    if study["study_id"] is None:
        row = connection.execute(
            "SELECT * FROM studies WHERE canonical_key = ?", (study["canonical_key"],)
        ).fetchone()
        if row is not None:
            raise ValueError("existing study must be addressed by study_id")
        return None, None
    row = connection.execute(
        "SELECT * FROM studies WHERE study_id = ?", (study["study_id"],)
    ).fetchone()
    if row is None:
        raise FileNotFoundError(f"experiment study does not exist: {study['study_id']}")
    if row["canonical_key"] != study["canonical_key"]:
        raise ValueError("study canonical_key does not match stored identity")
    head = connection.execute(
        "SELECT active_design_revision_id FROM study_heads WHERE study_id = ?",
        (study["study_id"],),
    ).fetchone()
    return row, None if head is None else str(head["active_design_revision_id"])


def _preview_normalized_plan(
    paths: ControllerPaths,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    target = ensure_registry(paths)
    with _connect(target.database_path) as connection:
        study_row, active_head = _active_study(connection, plan)
        expected = plan["expected_active_design_revision_id"]
        if expected != active_head:
            raise RuntimeError("active design revision conflict")
        active_points: dict[str, sqlite3.Row] = {}
        all_points_by_key: dict[str, sqlite3.Row] = {}
        if study_row is not None:
            all_points_by_key = {
                str(row["canonical_key"]): row
                for row in connection.execute(
                    "SELECT point_id, canonical_key FROM points WHERE study_id = ?",
                    (study_row["study_id"],),
                )
            }
        if active_head is not None:
            active_points = {
                str(row["point_id"]): row
                for row in connection.execute(
                    """SELECT dp.point_id, dp.point_revision_id, pr.point_revision_digest, p.canonical_key
                       FROM design_points dp
                       JOIN point_revisions pr ON pr.point_revision_id = dp.point_revision_id
                       JOIN points p ON p.point_id = dp.point_id
                       WHERE dp.design_revision_id = ?""",
                    (active_head,),
                )
            }
        classifications: list[dict[str, Any]] = []
        candidate_ids: set[str] = set()
        for point in plan["points"]:
            point_id = point["point_id"]
            if point_id is None:
                if point["canonical_key"] in all_points_by_key:
                    raise ValueError(
                        f"existing point {point['canonical_key']!r} must be addressed by point_id"
                    )
                classification = "new"
                old_revision_id = None
            else:
                stored = connection.execute(
                    "SELECT point_id, study_id, canonical_key FROM points WHERE point_id = ?",
                    (point_id,),
                ).fetchone()
                if (
                    stored is None
                    or study_row is None
                    or stored["study_id"] != study_row["study_id"]
                ):
                    raise ValueError(
                        f"point does not belong to the selected study: {point_id}"
                    )
                if stored["canonical_key"] != point["canonical_key"]:
                    raise ValueError(
                        f"point canonical_key does not match stored identity: {point_id}"
                    )
                candidate_ids.add(point_id)
                current = active_points.get(point_id)
                old_revision_id = (
                    None if current is None else str(current["point_revision_id"])
                )
                if current is None:
                    classification = "new"
                elif current["point_revision_digest"] == point["point_revision_digest"]:
                    classification = "unchanged"
                else:
                    classification = "stale"
            classifications.append(
                {
                    "classification": classification,
                    "point_id": point_id,
                    "canonical_key": point["canonical_key"],
                    "old_point_revision_id": old_revision_id,
                    "point_revision_digest": point["point_revision_digest"],
                }
            )
        for point_id, current in active_points.items():
            if point_id not in candidate_ids:
                classifications.append(
                    {
                        "classification": "archived",
                        "point_id": point_id,
                        "canonical_key": current["canonical_key"],
                        "old_point_revision_id": current["point_revision_id"],
                        "point_revision_digest": current["point_revision_digest"],
                    }
                )
    counts = Counter(item["classification"] for item in classifications)
    impact = {
        "counts": {
            key: counts.get(key, 0) for key in ("unchanged", "new", "stale", "archived")
        },
        "items": classifications,
    }
    impact_digest = contract_digest(
        {
            "study_id": plan["study"]["study_id"],
            "study_key": plan["study"]["canonical_key"],
            "expected_active_design_revision_id": active_head,
            "plan_digest": plan["plan_digest"],
            "impact": impact,
        }
    )
    return {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "project_id": paths.project_id,
        "current_active_design_revision_id": active_head,
        "plan_digest": plan["plan_digest"],
        "impact_digest": impact_digest,
        "impact": impact,
        "normalized_plan": plan,
    }


def preview_plan(paths: ControllerPaths, value: object) -> dict[str, Any]:
    plan = normalize_experiment_plan(value)
    with _experiment_lock(paths):
        _catch_up_locked(paths)
        return _preview_normalized_plan(paths, plan)


def publish_plan(
    paths: ControllerPaths,
    value: object,
    *,
    request_id: str,
    expected_impact_digest: str | None = None,
) -> dict[str, Any]:
    if (
        not request_id
        or len(request_id) > 256
        or any(character in request_id for character in ("\x00", "\n", "\r"))
    ):
        raise ValueError("experiment publication request_id is invalid")
    normalized_plan = normalize_experiment_plan(value)
    publication_request_digest = contract_digest(
        {
            "plan": normalized_plan,
            "expected_impact_digest": expected_impact_digest,
        }
    )
    with _experiment_lock(paths):
        target = ensure_registry(paths)
        _catch_up_locked(paths)
        for existing_event in _journal_events(target):
            if existing_event.get("request_id") != request_id:
                continue
            if (
                existing_event.get("event_type") != "plan_published"
                or existing_event.get("request_digest") != publication_request_digest
            ):
                raise RuntimeError(
                    "experiment request id was reused with different content"
                )
            _apply_event(paths, existing_event)
            existing_payload = dict(existing_event["payload"])
            existing_plan = dict(existing_payload["plan"])
            existing_study = dict(existing_plan["study"])
            return {
                "published": False,
                "study_id": existing_study["study_id"],
                "design_revision_id": existing_payload["design_revision_id"],
                "event_id": existing_event["event_id"],
                "journal_sequence": existing_event["sequence"],
                "plan_digest": existing_plan["plan_digest"],
                "impact_digest": existing_payload["impact_digest"],
                "impact": existing_payload["impact"],
            }

        preview = _preview_normalized_plan(paths, normalized_plan)
        if (
            expected_impact_digest is not None
            and expected_impact_digest != preview["impact_digest"]
        ):
            raise RuntimeError("plan impact digest conflict")
        plan = dict(preview["normalized_plan"])
        with _connect(target.database_path) as connection:
            _study_row, active_head = _active_study(connection, plan)
            if active_head != preview["current_active_design_revision_id"]:
                raise RuntimeError("active design revision conflict")
            if active_head is not None:
                active = connection.execute(
                    "SELECT plan_digest FROM design_revisions WHERE design_revision_id = ?",
                    (active_head,),
                ).fetchone()
                if active is not None and active["plan_digest"] == plan["plan_digest"]:
                    return {
                        "published": False,
                        "design_revision_id": active_head,
                        **preview,
                    }
            study_id = plan["study"]["study_id"] or _new_id("study")
            design_revision_id = _new_id("design")
            plan["study"] = {**plan["study"], "study_id": study_id}
            active_by_id = {}
            if active_head is not None:
                active_by_id = {
                    str(row["point_id"]): row
                    for row in connection.execute(
                        """SELECT dp.point_id, dp.point_revision_id, pr.point_revision_digest
                           FROM design_points dp JOIN point_revisions pr ON pr.point_revision_id = dp.point_revision_id
                           WHERE dp.design_revision_id = ?""",
                        (active_head,),
                    )
                }
            materialized_points = []
            for point in plan["points"]:
                point_id = point["point_id"] or _new_id("point")
                current = active_by_id.get(point_id)
                if (
                    current is not None
                    and current["point_revision_digest"]
                    == point["point_revision_digest"]
                ):
                    point_revision_id = str(current["point_revision_id"])
                elif point["reuse_point_revision_id"] is not None:
                    historical = connection.execute(
                        "SELECT point_id, point_revision_digest FROM point_revisions WHERE point_revision_id = ?",
                        (point["reuse_point_revision_id"],),
                    ).fetchone()
                    if (
                        historical is None
                        or historical["point_id"] != point_id
                        or historical["point_revision_digest"]
                        != point["point_revision_digest"]
                    ):
                        raise ValueError(
                            "reuse_point_revision_id does not match the exact point revision"
                        )
                    point_revision_id = str(point["reuse_point_revision_id"])
                else:
                    point_revision_id = _new_id("pointrev")
                materialized_points.append(
                    {
                        **point,
                        "point_id": point_id,
                        "point_revision_id": point_revision_id,
                    }
                )
            plan["points"] = materialized_points
        payload = {
            "design_revision_id": design_revision_id,
            "plan": plan,
            "impact": preview["impact"],
            "impact_digest": preview["impact_digest"],
            "publication_request_digest": publication_request_digest,
        }
        event, created = _append_event_locked(
            paths,
            event_type="plan_published",
            payload=payload,
            request_id=request_id,
            request_digest=publication_request_digest,
        )
        _apply_event(paths, event)
        return {
            "published": created,
            "study_id": study_id,
            "design_revision_id": design_revision_id,
            "event_id": event["event_id"],
            "journal_sequence": event["sequence"],
            **{key: preview[key] for key in ("plan_digest", "impact_digest", "impact")},
        }


def _validate_binding_against_projection(
    connection: sqlite3.Connection, binding: Mapping[str, Any]
) -> None:
    for target in binding["targets"]:
        head = connection.execute(
            "SELECT active_design_revision_id FROM study_heads WHERE study_id = ?",
            (target["study_id"],),
        ).fetchone()
        if (
            head is None
            or head["active_design_revision_id"] != target["origin_design_revision_id"]
        ):
            raise RuntimeError("run binding does not target the active design revision")
        design = connection.execute(
            "SELECT plan_digest FROM design_revisions WHERE design_revision_id = ?",
            (target["origin_design_revision_id"],),
        ).fetchone()
        revision = connection.execute(
            """SELECT pr.point_id, pr.point_revision_digest, pr.setting_digest
               FROM design_points dp JOIN point_revisions pr ON pr.point_revision_id = dp.point_revision_id
               WHERE dp.design_revision_id = ? AND dp.point_revision_id = ?""",
            (target["origin_design_revision_id"], target["point_revision_id"]),
        ).fetchone()
        if design is None or design["plan_digest"] != target["plan_digest"]:
            raise ValueError("run binding plan digest mismatch")
        if revision is None or revision["point_id"] != target["point_id"]:
            raise ValueError("run binding point revision is not in the design")
        if (
            revision["point_revision_digest"] != target["point_revision_digest"]
            or revision["setting_digest"] != target["setting_digest"]
        ):
            raise ValueError("run binding point or setting digest mismatch")


def validate_binding(paths: ControllerPaths, value: object) -> dict[str, Any]:
    binding = normalize_run_binding(value)
    target = ensure_registry(paths)
    with _experiment_lock(paths):
        _catch_up_locked(paths)
        with _connect(target.database_path) as connection:
            _validate_binding_against_projection(connection, binding)
    return binding


@contextlib.contextmanager
def binding_submission_guard(
    paths: ControllerPaths,
    value: object,
) -> Iterator[dict[str, Any]]:
    binding = normalize_run_binding(value)
    target = ensure_registry(paths)
    with _experiment_lock(paths):
        _catch_up_locked(paths)
        with _connect(target.database_path) as connection:
            _validate_binding_against_projection(connection, binding)
        yield binding
        payload = {"binding": binding}
        event, _created = _append_event_locked(
            paths,
            event_type="binding_observed",
            payload=payload,
            request_id=str(binding["binding_id"]),
            request_digest=contract_digest(payload),
        )
        _apply_event(paths, event)


def ingest_binding(paths: ControllerPaths, value: object) -> dict[str, Any]:
    binding = normalize_run_binding(value)
    target = ensure_registry(paths)
    with _experiment_lock(paths):
        _catch_up_locked(paths)
        with _connect(target.database_path) as connection:
            _validate_binding_against_projection(connection, binding)
        payload = {"binding": binding}
        event, created = _append_event_locked(
            paths,
            event_type="binding_observed",
            payload=payload,
            request_id=str(binding["binding_id"]),
            request_digest=contract_digest(payload),
        )
        _apply_event(paths, event)
    return {
        "ingested": created,
        "binding_id": binding["binding_id"],
        "event_id": event["event_id"],
    }


def _result_eligibility(
    connection: sqlite3.Connection,
    manifest: Mapping[str, Any],
    *,
    verified_run_ids: frozenset[str],
    require_verified_sync: bool,
) -> dict[str, list[str]]:
    eligibility: dict[str, list[str]] = {}
    for result in manifest["results"]:
        reasons: list[str] = []
        study = connection.execute(
            "SELECT study_id FROM studies WHERE study_id = ?",
            (result["study_id"],),
        ).fetchone()
        point = connection.execute(
            "SELECT study_id FROM points WHERE point_id = ?",
            (result["point_id"],),
        ).fetchone()
        if study is None or point is None:
            raise ValueError("experiment result references an unknown study or point")
        design = connection.execute(
            "SELECT study_id, plan_digest, plan_json FROM design_revisions WHERE design_revision_id = ?",
            (result["origin_design_revision_id"],),
        ).fetchone()
        revision = connection.execute(
            "SELECT point_id, point_revision_digest, setting_digest, requirements_json FROM point_revisions WHERE point_revision_id = ?",
            (result["point_revision_id"],),
        ).fetchone()
        if revision is None:
            raise ValueError("experiment result references an unknown point revision")
        if point["study_id"] != result["study_id"]:
            reasons.append("study_point_mismatch")
        if design is None or design["plan_digest"] != result["plan_digest"]:
            reasons.append("plan_mismatch")
        elif design["study_id"] != result["study_id"]:
            reasons.append("study_design_mismatch")
        if revision["point_id"] != result["point_id"]:
            reasons.append("point_revision_mismatch")
        elif (
            revision["point_revision_digest"] != result["point_revision_digest"]
            or revision["setting_digest"] != result["setting_digest"]
        ):
            reasons.append("point_digest_mismatch")
        requirements = json.loads(revision["requirements_json"])
        metric_keys = {metric["key"] for metric in result["metrics"]}
        metric_catalog = {
            metric["key"]: metric
            for metric in (
                [] if design is None else json.loads(design["plan_json"])["metrics"]
            )
        }
        if metric_keys - set(metric_catalog):
            reasons.append("unknown_metric")
        for metric in result["metrics"]:
            catalog_entry = metric_catalog.get(metric["key"])
            if catalog_entry is None:
                continue
            metric_value = metric["value"]
            value_type = catalog_entry["value_type"]
            type_matches = {
                "number": isinstance(metric_value, (int, float))
                and not isinstance(metric_value, bool),
                "integer": isinstance(metric_value, int)
                and not isinstance(metric_value, bool),
                "string": isinstance(metric_value, str),
                "boolean": isinstance(metric_value, bool),
            }[value_type]
            if not type_matches:
                reasons.append("metric_type_mismatch")
        if set(requirements.get("required_metrics", [])) - metric_keys:
            reasons.append("required_metric_missing")
        if result["evidence"]["observation_count"] < int(
            requirements.get("minimum_observations", 0)
        ):
            reasons.append("insufficient_observations")
        artifact_roles = {artifact["role"] for artifact in result["artifacts"]}
        if set(requirements.get("required_artifact_roles", [])) - artifact_roles:
            reasons.append("required_artifact_missing")
        if set(requirements.get("required_checks", [])) - set(
            result["evidence"]["checks"]
        ):
            reasons.append("required_check_missing")
        for contribution in result["contributions"]:
            binding = connection.execute(
                "SELECT run_id, binding_digest FROM run_bindings WHERE binding_id = ?",
                (contribution["binding_id"],),
            ).fetchone()
            if binding is None:
                raise ValueError("experiment result references an unknown run binding")
            target = connection.execute(
                "SELECT contribution_role FROM run_binding_targets WHERE binding_id = ? AND point_revision_id = ? AND result_group_id = ?",
                (
                    contribution["binding_id"],
                    result["point_revision_id"],
                    result["result_group_id"],
                ),
            ).fetchone()
            if (
                binding["run_id"] != contribution["run_id"]
                or binding["binding_digest"] != contribution["binding_digest"]
                or target is None
                or target["contribution_role"] != contribution["role"]
            ):
                reasons.append("binding_mismatch")
            if require_verified_sync and contribution["run_id"] not in verified_run_ids:
                reasons.append("output_sync_unverified")
        eligibility[result["result_id"]] = sorted(set(reasons))
    contribution_run_ids = {
        contribution["run_id"]
        for result in manifest["results"]
        for contribution in result["contributions"]
    }
    if manifest["emitter_run_id"] not in contribution_run_ids:
        raise ValueError("experiment result emitter must be a declared contribution")
    return eligibility


def ingest_result(
    paths: ControllerPaths,
    value: object,
    *,
    verification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = normalize_experiment_result(value)
    producer_mode = manifest["producer"]["mode"]
    if verification is None:
        if producer_mode != "legacy_adapter":
            raise ValueError(
                "native experiment results must be ingested from verified output sync"
            )
        normalized_verification: dict[str, Any] = {"mode": "legacy_adapter"}
    else:
        normalized_verification = dict(verification)
        if set(normalized_verification) != {"mode", "receipts"}:
            raise ValueError("experiment result verification is invalid")
        if normalized_verification["mode"] != "output_sync":
            raise ValueError("experiment result verification mode is invalid")
        receipts = normalized_verification["receipts"]
        if not isinstance(receipts, Mapping) or not receipts:
            raise ValueError("experiment result verification requires sync receipts")
        normalized_receipts: dict[str, str] = {}
        for run_id, digest in receipts.items():
            if (
                not isinstance(run_id, str)
                or RUN_ID_RE.fullmatch(run_id) is None
                or not isinstance(digest, str)
                or DIGEST_RE.fullmatch(digest) is None
            ):
                raise ValueError("experiment result verification receipt is invalid")
            normalized_receipts[run_id] = digest
        normalized_verification["receipts"] = dict(sorted(normalized_receipts.items()))
    verified_run_ids = frozenset(normalized_verification.get("receipts", {}).keys())
    target = ensure_registry(paths)
    with _experiment_lock(paths):
        _catch_up_locked(paths)
        with _connect(target.database_path) as connection:
            for result in manifest["results"]:
                existing = connection.execute(
                    "SELECT manifest_id FROM results WHERE result_id = ?",
                    (result["result_id"],),
                ).fetchone()
                if (
                    existing is not None
                    and existing["manifest_id"] != manifest["manifest_id"]
                ):
                    raise RuntimeError(
                        "experiment result id was reused by another manifest"
                    )
            eligibility = _result_eligibility(
                connection,
                manifest,
                verified_run_ids=verified_run_ids,
                require_verified_sync=producer_mode == "native",
            )
        payload = {
            "manifest": manifest,
            "eligibility": eligibility,
            "verification": normalized_verification,
        }
        event, created = _append_event_locked(
            paths,
            event_type="result_ingested",
            payload=payload,
            request_id=str(manifest["manifest_id"]),
            request_digest=contract_digest(payload),
        )
        _apply_event(paths, event)
    return {
        "ingested": created,
        "manifest_id": manifest["manifest_id"],
        "event_id": event["event_id"],
        "results": [
            {
                "result_id": result["result_id"],
                "eligible": not eligibility[result["result_id"]],
                "ineligibility_reasons": eligibility[result["result_id"]],
            }
            for result in manifest["results"]
        ],
    }


def ingest_completed_sync_results(paths: ControllerPaths) -> dict[str, Any]:
    if not paths.config_path.is_file():
        return {"projected": 0, "errors": []}
    execution_paths = project_paths(paths.config_path)
    completed_by_run = {
        str(item["run_id"]): item
        for item in list_completed_syncs(execution_paths.registry_root)
    }
    projected = 0
    errors: list[dict[str, str]] = []
    for emitter_run_id, completed in completed_by_run.items():
        receipt = completed.get("receipt")
        if not isinstance(receipt, Mapping):
            continue
        verified_result = receipt.get("experiment_result")
        if not isinstance(verified_result, Mapping):
            continue
        try:
            raw_manifest = verified_result.get("manifest")
            if not isinstance(raw_manifest, Mapping):
                raise ValueError("verified experiment result manifest is unavailable")
            if verified_result.get("canonical_sha256") != contract_digest(raw_manifest):
                raise ValueError("verified experiment result transport digest mismatch")
            manifest = normalize_experiment_result(raw_manifest)
            if manifest["emitter_run_id"] != emitter_run_id:
                raise ValueError("verified experiment result emitter mismatch")
            artifact_count = sum(
                len(result["artifacts"]) for result in manifest["results"]
            )
            if verified_result.get("artifact_count") != artifact_count:
                raise ValueError("verified experiment result artifact count mismatch")
            contribution_ids = {
                str(contribution["run_id"])
                for result in manifest["results"]
                for contribution in result["contributions"]
            }
            receipt_digests: dict[str, str] = {}
            for run_id in sorted(contribution_ids):
                contributing_sync = completed_by_run.get(run_id)
                if contributing_sync is None:
                    raise ValueError(
                        f"contributing run has no completed output sync: {run_id}"
                    )
                contributing_receipt = contributing_sync.get("receipt")
                if not isinstance(contributing_receipt, Mapping):
                    raise ValueError(f"contributing sync receipt is invalid: {run_id}")
                if contributing_receipt.get("verification") != "rsync_checksum_dry_run":
                    raise ValueError(f"contributing output is not verified: {run_id}")
                intent = contributing_sync.get("intent")
                if not isinstance(intent, Mapping) or intent.get(
                    "result_intent"
                ) not in {
                    "candidate",
                    "supporting",
                }:
                    raise ValueError(
                        f"contributing result intent is not eligible: {run_id}"
                    )
                run_manifest, run_state = load_current_run(execution_paths, run_id)
                if run_state["status"] != "succeeded":
                    raise ValueError(f"contributing run did not succeed: {run_id}")
                frozen_binding = run_manifest.get("experiment_binding")
                if frozen_binding is None:
                    raise ValueError(
                        f"contributing run has no frozen binding: {run_id}"
                    )
                normalized_binding = normalize_run_binding(frozen_binding)
                matching = [
                    contribution
                    for result in manifest["results"]
                    for contribution in result["contributions"]
                    if contribution["run_id"] == run_id
                ]
                if any(
                    contribution["binding_id"] != normalized_binding["binding_id"]
                    or contribution["binding_digest"]
                    != normalized_binding["binding_digest"]
                    for contribution in matching
                ):
                    raise ValueError(f"contributing frozen binding mismatch: {run_id}")
                receipt_digests[run_id] = contract_digest(contributing_sync)
            emitter_intent = completed.get("intent")
            if (
                not isinstance(emitter_intent, Mapping)
                or emitter_intent.get("result_intent") != "candidate"
            ):
                raise ValueError("experiment result emitter must have candidate intent")
            outcome = ingest_result(
                paths,
                manifest,
                verification={"mode": "output_sync", "receipts": receipt_digests},
            )
            projected += int(bool(outcome["ingested"]))
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            errors.append({"run_id": emitter_run_id, "error": str(exc)})
    return {"projected": projected, "errors": errors}


def record_acceptance(paths: ControllerPaths, value: object) -> dict[str, Any]:
    acceptance = normalize_acceptance_request(value)
    target = ensure_registry(paths)
    with _experiment_lock(paths):
        _catch_up_locked(paths)
        with _connect(target.database_path) as connection:
            result = connection.execute(
                "SELECT point_revision_id, eligible FROM results WHERE result_id = ?",
                (acceptance["result_id"],),
            ).fetchone()
            if (
                result is None
                or result["point_revision_id"] != acceptance["point_revision_id"]
            ):
                raise ValueError("acceptance result does not match the point revision")
            if acceptance["action"] != "revoke" and not bool(result["eligible"]):
                raise ValueError(
                    "ineligible result cannot receive an acceptance decision"
                )
            current = connection.execute(
                "SELECT acceptance_id, result_id FROM accepted_result_heads WHERE point_revision_id = ?",
                (acceptance["point_revision_id"],),
            ).fetchone()
            current_id = None if current is None else str(current["acceptance_id"])
            if (
                acceptance["action"] == "reject"
                and current is not None
                and current["result_id"] == acceptance["result_id"]
            ):
                raise ValueError("an accepted result must be revoked, not rejected")
            if current_id != acceptance["expected_current_acceptance_id"]:
                raise RuntimeError("accepted result revision conflict")
        acceptance["acceptance_id"] = acceptance["acceptance_id"] or _new_id(
            "acceptance"
        )
        payload = {"acceptance": acceptance}
        event, created = _append_event_locked(
            paths,
            event_type="acceptance_recorded",
            payload=payload,
            request_id=str(acceptance["acceptance_id"]),
            request_digest=contract_digest(payload),
        )
        _apply_event(paths, event)
    return {
        "recorded": created,
        "acceptance_id": acceptance["acceptance_id"],
        "event_id": event["event_id"],
    }


def _cursor_encode(value: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(canonical_json_bytes(value)).decode().rstrip("=")


def _cursor_decode(value: str) -> dict[str, Any]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(value + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid experiment query cursor") from exc
    if not isinstance(decoded, dict):
        raise ValueError("invalid experiment query cursor")
    return decoded


def _query_context(connection: sqlite3.Connection) -> tuple[str, int]:
    epoch = connection.execute(
        "SELECT value FROM registry_meta WHERE key = 'registry_epoch'"
    ).fetchone()[0]
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) FROM registry_events"
    ).fetchone()
    return str(epoch), int(row[0])


def _study_selector(
    connection: sqlite3.Connection, selector: Mapping[str, Any]
) -> sqlite3.Row:
    if selector.get("study_id") is not None:
        row = connection.execute(
            "SELECT * FROM studies WHERE study_id = ?", (selector["study_id"],)
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM studies WHERE canonical_key = ?",
            (selector["canonical_key"],),
        ).fetchone()
    if row is None:
        raise FileNotFoundError("experiment study does not exist")
    return row


def _plan_for_head(
    connection: sqlite3.Connection, study_id: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    row = connection.execute(
        """SELECT dr.design_revision_id, dr.plan_json, dr.impact_json
           FROM study_heads sh JOIN design_revisions dr ON dr.design_revision_id = sh.active_design_revision_id
           WHERE sh.study_id = ?""",
        (study_id,),
    ).fetchone()
    if row is None:
        raise FileNotFoundError("experiment study has no active design revision")
    return (
        str(row["design_revision_id"]),
        json.loads(row["plan_json"]),
        json.loads(row["impact_json"]),
    )


def _authoritative_run_statuses(paths: ControllerPaths) -> dict[str, str]:
    statuses: dict[str, str] = {}
    queue_mapping = {
        "queued": "queued",
        "dispatching": "running",
        "failed": "failed",
        "stopped": "failed",
    }
    for job, state in list_jobs(paths):
        mapped = queue_mapping.get(str(state["status"]))
        if mapped is not None:
            statuses[str(job["run_id"])] = mapped
    if not paths.config_path.is_file():
        return statuses
    execution_paths = project_paths(paths.config_path)
    if not execution_paths.runs_dir.is_dir():
        return statuses
    execution_mapping = {
        "registered": "running",
        "running": "running",
        "succeeded": "succeeded",
        "failed": "failed",
        "stopped": "failed",
    }
    for entry in execution_paths.runs_dir.glob("rr-*"):
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            _manifest, state = load_current_run(execution_paths, entry.name)
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            continue
        mapped = execution_mapping.get(str(state["status"]))
        if mapped is not None:
            statuses[entry.name] = mapped
    return statuses


def _result_metric_readings(
    connection: sqlite3.Connection,
    result_id: str,
) -> dict[str, Any]:
    readings: dict[str, Any] = {}
    for metric in connection.execute(
        "SELECT metric_key, value_json, interval_json FROM result_metrics WHERE result_id = ?",
        (result_id,),
    ):
        readings[str(metric["metric_key"])] = {
            "value": json.loads(metric["value_json"]),
            "interval": (
                None
                if metric["interval_json"] is None
                else json.loads(metric["interval_json"])
            ),
        }
    return readings


def _point_status(
    connection: sqlite3.Connection,
    point_id: str,
    point_revision_id: str,
    run_statuses: Mapping[str, str],
) -> tuple[str, int, bool]:
    accepted = connection.execute(
        "SELECT result_id FROM accepted_result_heads WHERE point_revision_id = ?",
        (point_revision_id,),
    ).fetchone()
    candidate_count = int(
        connection.execute(
            """SELECT COUNT(*)
               FROM results r
               WHERE r.point_revision_id = ? AND r.eligible = 1
                 AND COALESCE(
                   (
                     SELECT a.action
                     FROM acceptances a
                     JOIN registry_events re ON re.event_id = a.event_id
                     WHERE a.result_id = r.result_id
                     ORDER BY re.sequence DESC
                     LIMIT 1
                   ),
                   ''
                 ) != 'reject'""",
            (point_revision_id,),
        ).fetchone()[0]
    )
    if accepted is not None:
        return "complete", candidate_count, False
    bound_statuses = {
        run_statuses.get(str(row["run_id"]), "unknown")
        for row in connection.execute(
            """SELECT rb.run_id FROM run_binding_targets rbt
               JOIN run_bindings rb ON rb.binding_id = rbt.binding_id
               WHERE rbt.point_revision_id = ?""",
            (point_revision_id,),
        )
    }
    if "running" in bound_statuses:
        return "running", candidate_count, False
    if "queued" in bound_statuses:
        return "queued", candidate_count, False
    if candidate_count:
        return "review", candidate_count, False
    if "failed" in bound_statuses:
        return "failed", candidate_count, False
    stale = (
        connection.execute(
            """SELECT 1 FROM point_revisions pr
           JOIN accepted_result_heads ar ON ar.point_revision_id = pr.point_revision_id
           WHERE pr.point_id = ? AND pr.point_revision_id != ? LIMIT 1""",
            (point_id, point_revision_id),
        ).fetchone()
        is not None
    )
    return ("stale" if stale else "planned"), candidate_count, stale


def _study_item(
    connection: sqlite3.Connection,
    study: sqlite3.Row,
    event_cursor: int,
    run_statuses: Mapping[str, str],
) -> dict[str, Any]:
    active_revision_id, plan, impact = _plan_for_head(
        connection, str(study["study_id"])
    )
    name = _latest_name(
        connection, "study_names", str(study["study_id"]), "display_name"
    )
    counts: Counter[str] = Counter()
    for row in connection.execute(
        "SELECT dp.point_id, dp.point_revision_id FROM design_points dp WHERE dp.design_revision_id = ? ORDER BY dp.plan_order",
        (active_revision_id,),
    ):
        status, _candidates, _stale = _point_status(
            connection,
            str(row["point_id"]),
            str(row["point_revision_id"]),
            run_statuses,
        )
        counts[status] += 1
    return {
        "study_id": study["study_id"],
        "canonical_key": study["canonical_key"],
        "display_name": "" if name is None else name["display_name"],
        "description": "" if name is None else name["description"],
        "active_design_revision_id": active_revision_id,
        "previous_design_revision_id": plan.get("expected_active_design_revision_id"),
        "plan_digest": plan["plan_digest"],
        "dimensions": plan["dimensions"],
        "metrics": plan["metrics"],
        "presentation": plan["presentation"],
        "impact": impact["counts"],
        "status_counts": {
            key: counts.get(key, 0)
            for key in (
                "complete",
                "running",
                "queued",
                "review",
                "failed",
                "stale",
                "planned",
            )
        },
        "point_count": sum(counts.values()),
        "event_cursor": event_cursor,
    }


def _point_items(
    connection: sqlite3.Connection,
    study_id: str,
    query: Mapping[str, Any],
    run_statuses: Mapping[str, str],
) -> tuple[str, list[dict[str, Any]]]:
    active_revision_id, plan, _impact = _plan_for_head(connection, study_id)
    metric_catalog = {item["key"]: item for item in plan["metrics"]}
    dimension_keys = {item["key"] for item in plan["dimensions"]}
    unknown_dimensions = set(query["filters"]["dimensions"]) - dimension_keys
    if unknown_dimensions:
        raise ValueError(
            "experiment query filters unknown dimensions: "
            + ", ".join(sorted(unknown_dimensions))
        )
    rows = connection.execute(
        """SELECT dp.plan_order, p.point_id, p.canonical_key, dp.point_revision_id,
                  pr.point_revision_digest, pr.setting_digest, pr.dimensions_json,
                  pr.requirements_json
           FROM design_points dp
           JOIN points p ON p.point_id = dp.point_id
           JOIN point_revisions pr ON pr.point_revision_id = dp.point_revision_id
           WHERE dp.design_revision_id = ? ORDER BY dp.plan_order, p.point_id""",
        (active_revision_id,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    filters = query["filters"]
    for row in rows:
        dimensions = json.loads(row["dimensions_json"])
        if filters["canonical_key_prefix"] and not str(row["canonical_key"]).startswith(
            filters["canonical_key_prefix"]
        ):
            continue
        if any(
            dimensions.get(key) not in values
            for key, values in filters["dimensions"].items()
        ):
            continue
        status, candidate_count, stale = _point_status(
            connection,
            str(row["point_id"]),
            str(row["point_revision_id"]),
            run_statuses,
        )
        if filters["status"] and status not in filters["status"]:
            continue
        name = _latest_name(
            connection, "point_names", str(row["point_id"]), "display_name"
        )
        accepted = connection.execute(
            """SELECT ar.acceptance_id, ar.result_id, r.observation_count FROM accepted_result_heads ar
               JOIN results r ON r.result_id = ar.result_id WHERE ar.point_revision_id = ?""",
            (row["point_revision_id"],),
        ).fetchone()
        metrics = (
            {}
            if accepted is None
            else _result_metric_readings(connection, str(accepted["result_id"]))
        )
        run_rows = connection.execute(
            """SELECT rb.run_id, rbt.contribution_role AS role
               FROM run_binding_targets rbt
               JOIN run_bindings rb ON rb.binding_id = rbt.binding_id
               WHERE rbt.point_revision_id = ? ORDER BY rb.run_id LIMIT 20""",
            (row["point_revision_id"],),
        ).fetchall()
        items.append(
            {
                "plan_order": row["plan_order"],
                "point_id": row["point_id"],
                "point_revision_id": row["point_revision_id"],
                "canonical_key": row["canonical_key"],
                "display_name": "" if name is None else name["display_name"],
                "dimensions": dimensions,
                "status": status,
                "metrics": metrics,
                "accepted_acceptance_id": None
                if accepted is None
                else accepted["acceptance_id"],
                "accepted_result_id": None
                if accepted is None
                else accepted["result_id"],
                "observation_count": None
                if accepted is None
                else accepted["observation_count"],
                "candidate_count": candidate_count,
                "has_stale_history": stale,
                "stale_reason": "point_revision_changed" if stale else None,
                "setting_digest": row["setting_digest"],
                "point_revision_digest": row["point_revision_digest"],
                "requirements": json.loads(row["requirements_json"]),
                "runs": [
                    {
                        "run_id": run["run_id"],
                        "role": run["role"],
                        "status": run_statuses.get(str(run["run_id"]), "unknown"),
                    }
                    for run in run_rows
                ],
                "metric_catalog": metric_catalog,
            }
        )
    return active_revision_id, items


def _point_history_items(
    connection: sqlite3.Connection,
    study_id: str,
    selector: Mapping[str, Any],
    run_statuses: Mapping[str, str],
) -> tuple[str, list[dict[str, Any]]]:
    active_revision_id, plan, _impact = _plan_for_head(connection, study_id)
    if selector["point_revision_id"] is not None:
        point = connection.execute(
            """SELECT p.point_id, p.canonical_key FROM point_revisions pr
               JOIN points p ON p.point_id = pr.point_id
               WHERE pr.point_revision_id = ? AND p.study_id = ?""",
            (selector["point_revision_id"], study_id),
        ).fetchone()
    elif selector["point_id"] is not None:
        point = connection.execute(
            "SELECT point_id, canonical_key FROM points WHERE point_id = ? AND study_id = ?",
            (selector["point_id"], study_id),
        ).fetchone()
    else:
        point = connection.execute(
            "SELECT point_id, canonical_key FROM points WHERE canonical_key = ? AND study_id = ?",
            (selector["canonical_key"], study_id),
        ).fetchone()
    if point is None:
        raise FileNotFoundError("experiment point does not exist")
    active = connection.execute(
        "SELECT point_revision_id FROM design_points WHERE design_revision_id = ? AND point_id = ?",
        (active_revision_id, point["point_id"]),
    ).fetchone()
    active_point_revision_id = (
        None if active is None else str(active["point_revision_id"])
    )
    name = _latest_name(
        connection, "point_names", str(point["point_id"]), "display_name"
    )
    metric_catalog = {item["key"]: item for item in plan["metrics"]}
    rows = connection.execute(
        """SELECT pr.*, re.sequence AS revision_event_sequence
           FROM point_revisions pr
           LEFT JOIN registry_events re ON re.event_id = pr.created_event_id
           WHERE pr.point_id = ?
           ORDER BY re.sequence, pr.point_revision_id""",
        (point["point_id"],),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        point_revision_id = str(row["point_revision_id"])
        is_active = point_revision_id == active_point_revision_id
        if is_active:
            status, candidate_count, stale = _point_status(
                connection,
                str(point["point_id"]),
                point_revision_id,
                run_statuses,
            )
        else:
            status = "archived"
            candidate_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM results WHERE point_revision_id = ? AND eligible = 1",
                    (point_revision_id,),
                ).fetchone()[0]
            )
            stale = False
        accepted = connection.execute(
            """SELECT ar.acceptance_id, ar.result_id, r.observation_count
               FROM accepted_result_heads ar
               JOIN results r ON r.result_id = ar.result_id
               WHERE ar.point_revision_id = ?""",
            (point_revision_id,),
        ).fetchone()
        metrics: dict[str, Any] = {}
        if accepted is not None:
            for metric in connection.execute(
                "SELECT metric_key, value_json, interval_json FROM result_metrics WHERE result_id = ?",
                (accepted["result_id"],),
            ):
                metrics[str(metric["metric_key"])] = {
                    "value": json.loads(metric["value_json"]),
                    "interval": (
                        None
                        if metric["interval_json"] is None
                        else json.loads(metric["interval_json"])
                    ),
                }
        run_rows = connection.execute(
            """SELECT rb.run_id, rbt.contribution_role AS role
               FROM run_binding_targets rbt
               JOIN run_bindings rb ON rb.binding_id = rbt.binding_id
               WHERE rbt.point_revision_id = ? ORDER BY rb.run_id LIMIT 20""",
            (point_revision_id,),
        ).fetchall()
        candidates = connection.execute(
            """SELECT result_id, eligible, ineligibility_json, observation_count
               FROM results WHERE point_revision_id = ? ORDER BY result_id LIMIT 50""",
            (point_revision_id,),
        ).fetchall()
        artifact_rows = (
            connection.execute(
                """SELECT role, run_id, relative_path, sha256, media_type, size
                   FROM result_artifacts WHERE result_id = ?
                   ORDER BY role, relative_path LIMIT 100""",
                (accepted["result_id"],),
            ).fetchall()
            if accepted is not None
            else []
        )
        items.append(
            {
                "point_id": point["point_id"],
                "point_revision_id": point_revision_id,
                "canonical_key": point["canonical_key"],
                "display_name": "" if name is None else name["display_name"],
                "dimensions": json.loads(row["dimensions_json"]),
                "status": status,
                "metrics": metrics,
                "accepted_acceptance_id": (
                    None if accepted is None else accepted["acceptance_id"]
                ),
                "accepted_result_id": (
                    None if accepted is None else accepted["result_id"]
                ),
                "observation_count": (
                    None if accepted is None else accepted["observation_count"]
                ),
                "candidate_count": candidate_count,
                "has_stale_history": stale,
                "stale_reason": "point_revision_changed" if stale else None,
                "setting_digest": row["setting_digest"],
                "point_revision_digest": row["point_revision_digest"],
                "requirements": json.loads(row["requirements_json"]),
                "runs": [
                    {
                        "run_id": run["run_id"],
                        "role": run["role"],
                        "status": run_statuses.get(str(run["run_id"]), "unknown"),
                    }
                    for run in run_rows
                ],
                "metric_catalog": metric_catalog,
                "result_history": [
                    {
                        "result_id": candidate["result_id"],
                        "eligible": bool(candidate["eligible"]),
                        "ineligibility_reasons": json.loads(
                            candidate["ineligibility_json"]
                        ),
                        "observation_count": candidate["observation_count"],
                    }
                    for candidate in candidates
                ],
                "artifacts": [dict(artifact) for artifact in artifact_rows],
                "is_active": is_active,
                "revision_event_sequence": row["revision_event_sequence"],
            }
        )
    return active_revision_id, items


def _paginate(
    items: list[dict[str, Any]], query: Mapping[str, Any], epoch: str
) -> tuple[list[dict[str, Any]], str | None]:
    digest_value = contract_digest(
        {key: value for key, value in query.items() if key != "page"}
    )
    start = 0
    cursor = query["page"]["cursor"]
    if cursor is not None:
        decoded = _cursor_decode(cursor)
        if decoded.get("epoch") != epoch or decoded.get("query_digest") != digest_value:
            raise RuntimeError(
                "experiment query cursor expired or does not match the query"
            )
        start = int(decoded.get("offset", 0))
    limit = int(query["page"]["limit"])
    page = items[start : start + limit]
    next_cursor = None
    if start + len(page) < len(items):
        next_cursor = _cursor_encode(
            {"epoch": epoch, "query_digest": digest_value, "offset": start + len(page)}
        )
    return page, next_cursor


def _query_registry_locked(
    paths: ControllerPaths,
    query: Mapping[str, Any],
) -> dict[str, Any]:
    target = ensure_registry(paths)
    run_statuses = _authoritative_run_statuses(paths)
    with _connect(target.database_path) as connection:
        epoch, event_cursor = _query_context(connection)
        operation = query["operation"]
        active_design_revision_id = None
        dashboard_studies: list[dict[str, Any]] | None = None
        if operation == "study_list":
            all_items = [
                _study_item(connection, row, event_cursor, run_statuses)
                for row in connection.execute(
                    "SELECT * FROM studies ORDER BY canonical_key, study_id"
                )
            ]
        else:
            study = _study_selector(connection, query["study"])
            if operation == "dashboard":
                dashboard_studies = [
                    _study_item(connection, row, event_cursor, run_statuses)
                    for row in connection.execute(
                        "SELECT * FROM studies ORDER BY canonical_key, study_id"
                    )
                ]
            if operation == "study_status":
                all_items = [_study_item(connection, study, event_cursor, run_statuses)]
                active_design_revision_id = all_items[0]["active_design_revision_id"]
            elif operation == "point_history":
                active_design_revision_id, all_items = _point_history_items(
                    connection,
                    str(study["study_id"]),
                    query["point"],
                    run_statuses,
                )
            else:
                active_design_revision_id, points = _point_items(
                    connection,
                    str(study["study_id"]),
                    query,
                    run_statuses,
                )
                if operation in {"dashboard", "point_list", "rerun_list"}:
                    all_items = (
                        points
                        if operation in {"dashboard", "point_list"}
                        else [item for item in points if item["status"] != "complete"]
                    )
                else:
                    selector = query["point"]
                    all_items = [
                        item
                        for item in points
                        if (
                            selector["point_id"] is not None
                            and item["point_id"] == selector["point_id"]
                        )
                        or (
                            selector["point_revision_id"] is not None
                            and item["point_revision_id"]
                            == selector["point_revision_id"]
                        )
                        or (
                            selector["canonical_key"] is not None
                            and item["canonical_key"] == selector["canonical_key"]
                        )
                    ]
                    if not all_items:
                        raise FileNotFoundError(
                            "experiment point does not exist in the active design"
                        )
                    if operation == "point_detail":
                        point = all_items[0]
                        candidates = connection.execute(
                            """SELECT r.result_id, r.eligible, r.ineligibility_json,
                                      r.observation_count,
                                      (
                                        SELECT a.action
                                        FROM acceptances a
                                        JOIN registry_events re ON re.event_id = a.event_id
                                        WHERE a.result_id = r.result_id
                                        ORDER BY re.sequence DESC
                                        LIMIT 1
                                      ) AS decision_action
                               FROM results r
                               WHERE r.point_revision_id = ?
                               ORDER BY r.result_id
                               LIMIT 50""",
                            (point["point_revision_id"],),
                        ).fetchall()
                        point["result_history"] = [
                            {
                                "result_id": row["result_id"],
                                "eligible": bool(row["eligible"]),
                                "ineligibility_reasons": json.loads(
                                    row["ineligibility_json"]
                                ),
                                "observation_count": row["observation_count"],
                                "decision_action": row["decision_action"],
                                "metrics": _result_metric_readings(
                                    connection,
                                    str(row["result_id"]),
                                ),
                                "source_run_ids": [
                                    str(result_run["run_id"])
                                    for result_run in connection.execute(
                                        "SELECT DISTINCT run_id FROM result_runs WHERE result_id = ? ORDER BY run_id",
                                        (row["result_id"],),
                                    )
                                ],
                            }
                            for row in candidates
                        ]
                        artifact_rows = (
                            connection.execute(
                                "SELECT role, run_id, relative_path, sha256, media_type, size FROM result_artifacts WHERE result_id = ? ORDER BY role, relative_path LIMIT 100",
                                (point["accepted_result_id"],),
                            ).fetchall()
                            if point["accepted_result_id"]
                            else []
                        )
                        point["artifacts"] = [dict(row) for row in artifact_rows]
        changed_since = query["changed_since"]
        if changed_since is not None and int(changed_since) >= event_cursor:
            all_items = []
        projection_fields = tuple(query["fields"])
        if not projection_fields and operation in {"dashboard", "point_list"}:
            projection_fields = DEFAULT_POINT_LIST_FIELDS
        elif not projection_fields and operation == "rerun_list":
            projection_fields = DEFAULT_RERUN_FIELDS
        requested_fields = set(projection_fields)
        if projection_fields:
            allowed_fields = (
                STUDY_QUERY_FIELDS
                if operation in {"study_list", "study_status"}
                else POINT_QUERY_FIELDS
            )
            unknown_fields = requested_fields - allowed_fields
            if unknown_fields:
                raise ValueError(
                    "experiment query requests unknown fields: "
                    + ", ".join(sorted(unknown_fields))
                )
            all_items = [
                {key: item[key] for key in projection_fields if key in item}
                for item in all_items
            ]
        page, next_cursor = _paginate(all_items, query, epoch)
        response = {
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "project_id": paths.project_id,
            "registry_epoch": epoch,
            "event_cursor": event_cursor,
            "active_design_revision_id": active_design_revision_id,
            "items": page,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
        }
        if dashboard_studies is not None:
            response["studies"] = dashboard_studies
    if len(canonical_json_bytes(response)) > MAX_QUERY_RESPONSE_BYTES:
        raise ValueError("experiment query response exceeds the serialized size limit")
    return response


def query_registry(paths: ControllerPaths, value: object) -> dict[str, Any]:
    query = normalize_experiment_query(value)
    with _experiment_lock(paths):
        _catch_up_locked(paths)
        return _query_registry_locked(paths, query)
