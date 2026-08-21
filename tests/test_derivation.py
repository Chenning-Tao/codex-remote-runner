from __future__ import annotations

from typing import Any

import pytest

from remote_runner._internal.derivation import (
    artifact_result_path,
    build_relation,
    derived_run_id,
    normalize_validator_key,
    receipt_identity_sha256,
    relation_identity,
    spec_digest,
    validate_relation,
)


SOURCE_RUN_ID = "rr-0123456789abcdef"
REVISION = "a" * 40


def relation(**overrides: Any) -> dict[str, Any]:
    payload = {
        "source_run_id": SOURCE_RUN_ID,
        "source_revision": REVISION,
        "source_server": "compute-a",
        "target_server": "archive",
        "target_path": "/srv/archive/artifacts/rr-0123456789abcdef",
        "receipt_sha256": "sha256:" + "b" * 64,
        "validator_key": "portable-smoke/v1",
        "result_relpath": "acceptance.json",
    }
    payload.update(overrides)
    return build_relation(**payload)


def digest(value: dict[str, Any], **overrides: Any) -> str:
    spec = {
        "label": "validate:portable-smoke/v1",
        "task_id": "validation/" + SOURCE_RUN_ID,
        "submitted_command_sha256": "sha256:" + "c" * 64,
        "minimum_cores": 1,
        "requested_cores": 1,
        "workload_class": "test",
        "output_relpath": "validation/rr-fedcba9876543210",
        "privacy": None,
        "eligible_servers": ["archive"],
    }
    spec.update(overrides)
    return spec_digest(value, **spec)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "key",
    [
        "Portable-Smoke/v1",
        "portable smoke",
        "/portable-smoke",
        "portable-smoke/",
        "portable//smoke",
        "",
        "x" * 129,
    ],
)
def test_validator_key_rejects_unsafe_values(key: str) -> None:
    with pytest.raises(ValueError):
        normalize_validator_key(key)


def test_validator_key_accepts_segmented_lowercase_keys() -> None:
    assert normalize_validator_key("portable-smoke/v1") == "portable-smoke/v1"
    assert normalize_validator_key("remote-runner.smoke_2/v10") == (
        "remote-runner.smoke_2/v10"
    )


def test_derived_run_id_is_deterministic_and_scoped() -> None:
    identity = {
        "project_id": "example",
        "source_run_id": SOURCE_RUN_ID,
        "validator_key": "portable-smoke/v1",
    }
    first = derived_run_id(**identity)

    assert first == derived_run_id(**identity)
    assert first != derived_run_id(**{**identity, "project_id": "other"})
    assert first != derived_run_id(**{**identity, "validator_key": "portable-smoke/v2"})
    assert first != derived_run_id(
        **{**identity, "source_run_id": "rr-fedcba9876543210"}
    )


def test_derived_run_id_matches_the_current_run_id_shape() -> None:
    run_id = derived_run_id(
        project_id="example",
        source_run_id=SOURCE_RUN_ID,
        validator_key="portable-smoke/v1",
    )

    assert len(run_id) == len("rr-") + 16
    assert run_id.startswith("rr-")
    assert set(run_id[3:]) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_revision": "a" * 39},
        {"source_revision": "A" * 40},
        {"source_run_id": "rr-not-hex"},
        {"target_path": "relative/path"},
        {"target_path": "/srv/archive/../escape"},
        {"receipt_sha256": "b" * 64},
        {"source_server": "bad server"},
        {"result_relpath": "/absolute.json"},
        {"result_relpath": "../acceptance.json"},
    ],
)
def test_relation_rejects_unusable_identities(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        relation(**overrides)


def test_validate_relation_rejects_unknown_and_missing_fields() -> None:
    payload = relation()
    payload["spec_sha256"] = digest(payload)

    assert validate_relation(payload) == payload

    with pytest.raises(ValueError, match="unsupported fields"):
        validate_relation({**payload, "reviewer": "someone"})
    with pytest.raises(ValueError, match="source_artifact has unsupported fields"):
        validate_relation(
            {
                **payload,
                "source_artifact": {**payload["source_artifact"], "size": 12},
            }
        )
    with pytest.raises(ValueError):
        validate_relation({key: value for key, value in payload.items() if key != "spec_sha256"})
    with pytest.raises(ValueError, match="unsupported derivation kind"):
        validate_relation({**payload, "kind": "benchmark"})
    with pytest.raises(ValueError, match="unsupported derivation schema"):
        validate_relation({**payload, "schema_version": 2})


def test_relation_identity_excludes_its_own_digest() -> None:
    payload = relation()
    payload["spec_sha256"] = digest(payload)

    assert "spec_sha256" not in relation_identity(payload)
    assert relation_identity(payload) == relation()


@pytest.mark.parametrize(
    "overrides",
    [
        {"label": "validate:other"},
        {"task_id": "validation/rr-fedcba9876543210"},
        {"submitted_command_sha256": "sha256:" + "d" * 64},
        {"minimum_cores": 2},
        {"requested_cores": 4},
        {"requested_cores": None},
        {"workload_class": "standard"},
        {"output_relpath": "validation/other"},
        {"privacy": "process-title"},
        {"eligible_servers": ["archive", "compute-a"]},
    ],
)
def test_spec_digest_covers_every_immutable_input(overrides: dict[str, Any]) -> None:
    base = relation()

    assert digest(base) != digest(base, **overrides)


def test_spec_digest_follows_the_frozen_identity() -> None:
    base = relation()

    assert digest(base) != digest(relation(validator_key="portable-smoke/v2"))
    assert digest(base) != digest(relation(receipt_sha256="sha256:" + "e" * 64))
    assert digest(base) == digest(build_relation(**{
        "source_run_id": SOURCE_RUN_ID,
        "source_revision": REVISION,
        "source_server": "compute-a",
        "target_server": "archive",
        "target_path": "/srv/archive/artifacts/rr-0123456789abcdef",
        "receipt_sha256": "sha256:" + "b" * 64,
        "validator_key": "portable-smoke/v1",
        "result_relpath": "acceptance.json",
    }))


def test_spec_digest_ignores_eligible_server_order() -> None:
    base = relation()

    assert digest(base, eligible_servers=["archive", "compute-a"]) == digest(
        base, eligible_servers=["compute-a", "archive"]
    )


def test_receipt_identity_digest_tracks_transport_identity() -> None:
    receipt = {
        "run_id": SOURCE_RUN_ID,
        "source_server": "compute-a",
        "source_path": "/srv/outputs/runs/source",
        "source_kind": "directory",
        "target_path": "/srv/archive/artifacts/rr-0123456789abcdef",
        "revision": REVISION,
        "authoritative_status": "succeeded",
        "terminal_at": "2026-08-20T00:00:00Z",
        "archived_at": "2026-08-20T00:05:00Z",
        "verification": "rsync_checksum_dry_run",
        "disposition": "copied_and_verified",
        "source_deletion_performed": False,
    }

    first = receipt_identity_sha256(receipt)

    assert first == receipt_identity_sha256({**receipt, "source_deletion_performed": True})
    assert first != receipt_identity_sha256({**receipt, "target_path": "/srv/archive/other"})
    with pytest.raises(ValueError):
        receipt_identity_sha256({**receipt, "verification": None})


def test_artifact_result_path_stays_under_the_artifact_root() -> None:
    payload = relation(result_relpath="reports/acceptance.json")

    assert artifact_result_path(payload, "/srv/archive/artifacts/rr-0123456789abcdef") == (
        "/srv/archive/artifacts/rr-0123456789abcdef/reports/acceptance.json"
    )
    with pytest.raises(ValueError):
        artifact_result_path(payload, "srv/archive/relative")
