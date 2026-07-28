from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from remote_runner._internal.controller import experiments
from remote_runner._internal.controller.experiments import (
    experiment_purge_blockers,
    experiment_paths,
    ingest_binding,
    ingest_result,
    preview_plan,
    publish_plan,
    query_registry,
    rebuild_registry,
    record_acceptance,
)
from remote_runner._internal.controller.registry import controller_paths
from remote_runner._internal.experiment_contracts import contract_digest


def test_contract_digest_golden_vector() -> None:
    assert (
        contract_digest(
            {
                "z": 3,
                "a": [True, None, "value"],
                "nested": {"b": 2, "a": 1},
            }
        )
        == "sha256:5924ca571f38d59ea5706253d47f876d77d4ab82f81046bbf3459c2d2adf1155"
    )


def plan(
    *,
    study_id: str | None = None,
    point_id: str | None = None,
    head: str | None = None,
    batch: int = 4,
) -> dict[str, object]:
    return {
        "kind": "experiment_plan",
        "schema_version": 1,
        "study": {
            "study_id": study_id,
            "canonical_key": "throughput-sweep",
            "display_name": "Throughput sweep",
            "aliases": [],
            "description": "Generic batch throughput study",
            "metadata": {},
        },
        "expected_active_design_revision_id": head,
        "dimensions": [
            {
                "key": "engine",
                "display_name": "Engine",
                "value_type": "string",
                "order": ["native"],
            },
            {
                "key": "batch",
                "display_name": "Batch",
                "value_type": "integer",
                "order": [batch],
            },
        ],
        "setting_components": [
            {"key": "runtime", "digest": "sha256:" + "1" * 64, "metadata": {}},
        ],
        "metrics": [
            {
                "key": "samples_per_second",
                "display_name": "Samples per second",
                "value_type": "number",
                "unit": "samples/s",
                "default_format": "integer",
            },
        ],
        "points": [
            {
                "point_id": point_id,
                "reuse_point_revision_id": None,
                "canonical_key": "native-batch",
                "display_name": f"Native / batch={batch}",
                "aliases": [],
                "dimensions": {"engine": "native", "batch": batch},
                "parameters": {"warmup": 3},
                "setting_dependencies": ["runtime"],
                "result_requirements": {
                    "required_metrics": ["samples_per_second"],
                    "minimum_observations": 100,
                    "required_artifact_roles": ["summary"],
                    "required_checks": ["stable_clock"],
                },
                "metadata": {},
            }
        ],
        "presentation": {
            "primary_metric": "samples_per_second",
            "results": {
                "dimensions": ["engine", "batch"],
                "metrics": ["samples_per_second"],
            },
            "curves": [
                {
                    "key": "throughput-by-batch",
                    "display_name": "Throughput by batch",
                    "metric": "samples_per_second",
                    "x_dimension": "batch",
                    "series_dimensions": ["engine"],
                    "scale": "linear",
                    "show_interval": True,
                }
            ],
            "matrix": {
                "row_dimension": "engine",
                "column_dimension": "batch",
                "facet_dimensions": [],
            },
        },
    }


def query(
    operation: str,
    *,
    study_id: str | None = None,
    point_id: str | None = None,
    point_revision_id: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": "experiment_query",
        "schema_version": 1,
        "operation": operation,
        "page": {"limit": 50, "cursor": None},
    }
    if study_id is not None:
        value["study"] = {"study_id": study_id}
    if point_id is not None:
        value["point"] = {"point_id": point_id}
    elif point_revision_id is not None:
        value["point"] = {"point_revision_id": point_revision_id}
    return value


def test_experiment_registry_projects_explicit_results_and_rebuilds(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    published = publish_plan(paths, plan(), request_id="publish-throughput")
    study_id = str(published["study_id"])
    design_id = str(published["design_revision_id"])

    studies = query_registry(paths, query("study_list"))
    assert studies["items"][0]["status_counts"]["planned"] == 1
    points = query_registry(paths, query("point_list", study_id=study_id))
    point = points["items"][0]
    assert point["accepted_acceptance_id"] is None
    large_page_query = query("point_list", study_id=study_id)
    large_page_query["page"] = {"limit": 500, "cursor": None}
    assert len(query_registry(paths, large_page_query)["items"]) == 1
    dashboard = query_registry(paths, query("dashboard", study_id=study_id))
    assert dashboard["studies"][0]["study_id"] == study_id
    assert dashboard["items"][0]["point_revision_id"] == point["point_revision_id"]
    too_large_page_query = query("point_list", study_id=study_id)
    too_large_page_query["page"] = {"limit": 501, "cursor": None}
    with pytest.raises(ValueError, match="between 1 and 500"):
        query_registry(paths, too_large_page_query)

    binding = {
        "kind": "run_binding",
        "schema_version": 1,
        "binding_id": "binding-0123456789abcdef",
        "run_id": "rr-0123456789abcdef",
        "source_revision": "a" * 40,
        "targets": [
            {
                "study_id": study_id,
                "origin_design_revision_id": design_id,
                "plan_digest": published["plan_digest"],
                "point_id": point["point_id"],
                "point_revision_id": point["point_revision_id"],
                "point_revision_digest": point["point_revision_digest"],
                "setting_digest": point["setting_digest"],
                "result_group_id": "throughput-primary",
                "contribution_role": "primary",
            }
        ],
        "result_manifest_relpath": "experiment-result.json",
        "expects_result_manifest": True,
        "metadata": {},
    }
    binding_result = ingest_binding(paths, binding)
    result_manifest = {
        "kind": "experiment_result",
        "schema_version": 1,
        "manifest_id": "result-manifest-0123456789abcdef",
        "emitter_run_id": "rr-0123456789abcdef",
        "producer": {"name": "benchmark", "version": "1.0", "mode": "native"},
        "results": [
            {
                "result_id": "result-0123456789abcdef",
                "study_id": study_id,
                "origin_design_revision_id": design_id,
                "plan_digest": published["plan_digest"],
                "point_id": point["point_id"],
                "point_revision_id": point["point_revision_id"],
                "point_revision_digest": point["point_revision_digest"],
                "setting_digest": point["setting_digest"],
                "result_group_id": "throughput-primary",
                "contributions": [
                    {
                        "run_id": "rr-0123456789abcdef",
                        "binding_id": binding_result["binding_id"],
                        "binding_digest": binding_result.get(
                            "binding_digest", binding.get("binding_digest")
                        ),
                        "role": "primary",
                        "replaces_run_id": None,
                    }
                ],
                "metrics": [
                    {
                        "key": "samples_per_second",
                        "value": 612.5,
                        "interval": {
                            "lower": 600.0,
                            "upper": 625.0,
                            "level": 0.95,
                            "method": "bootstrap",
                        },
                    },
                ],
                "evidence": {"observation_count": 500, "checks": ["stable_clock"]},
                "artifacts": [
                    {
                        "run_id": "rr-0123456789abcdef",
                        "role": "summary",
                        "relative_path": "summary.json",
                        "sha256": "sha256:" + "2" * 64,
                        "media_type": "application/json",
                        "size": 512,
                    }
                ],
                "metadata": {},
            }
        ],
    }
    # The normalizer computed this when the binding was ingested.
    from remote_runner._internal.experiment_contracts import normalize_run_binding

    result_manifest["results"][0]["contributions"][0]["binding_digest"] = (
        normalize_run_binding(binding)["binding_digest"]
    )
    with pytest.raises(
        ValueError,
        match="must be ingested from verified output sync",
    ):
        ingest_result(paths, result_manifest)
    ingested = ingest_result(
        paths,
        result_manifest,
        verification={
            "mode": "output_sync",
            "receipts": {
                "rr-0123456789abcdef": "sha256:" + "3" * 64,
            },
        },
    )
    assert ingested["results"] == [
        {
            "result_id": "result-0123456789abcdef",
            "eligible": True,
            "ineligibility_reasons": [],
        }
    ]
    retried_result = ingest_result(
        paths,
        result_manifest,
        verification={
            "mode": "output_sync",
            "receipts": {
                "rr-0123456789abcdef": "sha256:" + "3" * 64,
            },
        },
    )
    assert retried_result["ingested"] is False
    assert retried_result["event_id"] == ingested["event_id"]

    tampered_manifest = deepcopy(result_manifest)
    tampered_manifest["results"][0]["metrics"][0]["value"] = 700.0
    with pytest.raises(RuntimeError, match="request id was reused"):
        ingest_result(
            paths,
            tampered_manifest,
            verification={
                "mode": "output_sync",
                "receipts": {
                    "rr-0123456789abcdef": "sha256:" + "3" * 64,
                },
            },
        )

    purge_blocker = experiment_purge_blockers(
        paths,
        ["rr-0123456789abcdef"],
    )[0]
    assert purge_blocker["result_ids"] == ["result-0123456789abcdef"]
    assert purge_blocker["accepted_result_ids"] == []

    review = query_registry(paths, query("point_list", study_id=study_id))
    assert review["items"][0]["status"] == "review"
    rejected = record_acceptance(
        paths,
        {
            "point_revision_id": point["point_revision_id"],
            "result_id": "result-0123456789abcdef",
            "expected_current_acceptance_id": None,
            "action": "reject",
            "actor": "test-suite",
            "reason": "fixture requires another review pass",
            "policy": "manual",
        },
    )
    assert rejected["acceptance_id"].startswith("acceptance-")
    rejected_detail = query_registry(
        paths,
        query(
            "point_detail",
            study_id=study_id,
            point_revision_id=point["point_revision_id"],
        ),
    )
    assert rejected_detail["items"][0]["status"] == "planned"
    assert rejected_detail["items"][0]["candidate_count"] == 0
    assert (
        rejected_detail["items"][0]["result_history"][0]["decision_action"] == "reject"
    )
    assert rejected_detail["items"][0]["result_history"][0]["metrics"] == {
        "samples_per_second": {
            "value": 612.5,
            "interval": {
                "lower": 600.0,
                "upper": 625.0,
                "level": 0.95,
                "method": "bootstrap",
            },
        }
    }
    assert rejected_detail["items"][0]["result_history"][0]["source_run_ids"] == [
        "rr-0123456789abcdef"
    ]

    rebuilt_rejection = rebuild_registry(paths)
    assert rebuilt_rejection["event_count"] == 4
    after_rejected_rebuild = query_registry(
        paths,
        query(
            "point_detail",
            study_id=study_id,
            point_revision_id=point["point_revision_id"],
        ),
    )
    assert after_rejected_rebuild["items"][0]["status"] == "planned"
    assert (
        after_rejected_rebuild["items"][0]["result_history"][0]["decision_action"]
        == "reject"
    )

    accepted = record_acceptance(
        paths,
        {
            "point_revision_id": point["point_revision_id"],
            "result_id": "result-0123456789abcdef",
            "expected_current_acceptance_id": None,
            "action": "accept",
            "actor": "test-suite",
            "reason": "verified fixture",
            "policy": "manual",
        },
    )
    assert accepted["acceptance_id"].startswith("acceptance-")
    accepted_purge_blocker = experiment_purge_blockers(
        paths,
        ["rr-0123456789abcdef"],
    )[0]
    assert accepted_purge_blocker["accepted_result_ids"] == ["result-0123456789abcdef"]
    complete = query_registry(
        paths,
        query(
            "point_detail",
            study_id=study_id,
            point_revision_id=point["point_revision_id"],
        ),
    )
    assert complete["items"][0]["status"] == "complete"
    assert complete["items"][0]["accepted_acceptance_id"] == accepted["acceptance_id"]
    assert complete["items"][0]["metrics"]["samples_per_second"]["value"] == 612.5
    assert complete["items"][0]["artifacts"][0]["relative_path"] == "summary.json"
    with pytest.raises(ValueError, match="must be revoked"):
        record_acceptance(
            paths,
            {
                "point_revision_id": point["point_revision_id"],
                "result_id": "result-0123456789abcdef",
                "expected_current_acceptance_id": accepted["acceptance_id"],
                "action": "reject",
                "actor": "test-suite",
                "reason": "incorrect action for an accepted result",
                "policy": "manual",
            },
        )

    rebuilt = rebuild_registry(paths)
    assert rebuilt["event_count"] == 5
    after_rebuild = query_registry(paths, query("point_list", study_id=study_id))
    assert after_rebuild["items"][0]["status"] == "complete"
    assert after_rebuild["registry_epoch"] != complete["registry_epoch"]

    next_plan = plan(
        study_id=study_id,
        point_id=str(point["point_id"]),
        head=design_id,
        batch=8,
    )
    publish_plan(paths, next_plan, request_id="publish-next-revision")
    history = query_registry(
        paths,
        query(
            "point_history",
            study_id=study_id,
            point_id=str(point["point_id"]),
        ),
    )["items"]
    assert len(history) == 2
    historical = next(item for item in history if item["is_active"] is False)
    active = next(item for item in history if item["is_active"] is True)
    assert historical["accepted_result_id"] == "result-0123456789abcdef"
    assert active["accepted_result_id"] is None


def test_new_point_revision_is_stale_without_result_fallback(tmp_path: Path) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    first = publish_plan(paths, plan(), request_id="publish-1")
    study_id = str(first["study_id"])
    point = query_registry(paths, query("point_list", study_id=study_id))["items"][0]

    candidate = plan(
        study_id=study_id,
        point_id=str(point["point_id"]),
        head=str(first["design_revision_id"]),
        batch=8,
    )
    preview = preview_plan(paths, candidate)
    assert preview["impact"]["counts"] == {
        "unchanged": 0,
        "new": 0,
        "stale": 1,
        "archived": 0,
    }
    second = publish_plan(
        paths,
        candidate,
        request_id="publish-2",
        expected_impact_digest=preview["impact_digest"],
    )
    assert second["design_revision_id"] != first["design_revision_id"]
    current = query_registry(paths, query("point_list", study_id=study_id))["items"][0]
    assert current["point_revision_id"] != point["point_revision_id"]
    assert current["accepted_result_id"] is None


def test_plan_publication_retry_preserves_ids_and_projection_recovers(
    tmp_path: Path,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    document = plan()

    first = publish_plan(paths, document, request_id="publish-idempotent")
    retried = publish_plan(paths, document, request_id="publish-idempotent")

    assert retried["published"] is False
    assert retried["study_id"] == first["study_id"]
    assert retried["design_revision_id"] == first["design_revision_id"]
    assert retried["event_id"] == first["event_id"]

    with pytest.raises(RuntimeError, match="request id was reused"):
        publish_plan(
            paths,
            plan(batch=16),
            request_id="publish-idempotent",
        )

    target = experiment_paths(paths)
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{target.database_path}{suffix}")
        if path.exists():
            path.unlink()

    recovered = query_registry(paths, query("study_list"))

    assert recovered["items"][0]["study_id"] == first["study_id"]
    assert (
        recovered["items"][0]["active_design_revision_id"]
        == first["design_revision_id"]
    )


def test_plan_publication_retry_recovers_after_journal_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = controller_paths(tmp_path / "controller", "example")
    document = plan()
    original_apply = experiments._apply_event

    monkeypatch.setattr(
        experiments,
        "_apply_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("simulated projection interruption")
        ),
    )
    with pytest.raises(OSError, match="simulated projection interruption"):
        publish_plan(paths, document, request_id="publish-after-journal")

    target = experiment_paths(paths)
    assert len(list(target.journal_dir.glob("*.json"))) == 1

    monkeypatch.setattr(experiments, "_apply_event", original_apply)
    recovered = publish_plan(
        paths,
        document,
        request_id="publish-after-journal",
    )
    assert recovered["published"] is False
    studies = query_registry(paths, query("study_list"))
    assert studies["items"][0]["study_id"] == recovered["study_id"]
