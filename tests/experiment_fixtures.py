from __future__ import annotations

from typing import Any

from remote_runner._internal.controller.experiments import (
    ingest_binding,
    ingest_result,
    publish_plan,
    query_registry,
)
from remote_runner._internal.controller.registry import ControllerPaths
from remote_runner._internal.experiment_contracts import normalize_run_binding


def ingest_experiment_result_reference(
    paths: ControllerPaths,
    run_id: str,
) -> dict[str, Any]:
    token = run_id.removeprefix("rr-")
    published = publish_plan(
        paths,
        {
            "kind": "experiment_plan",
            "schema_version": 1,
            "study": {
                "canonical_key": f"purge-guard-{token}",
                "display_name": "Purge guard study",
            },
            "dimensions": [],
            "setting_components": [],
            "metrics": [
                {
                    "key": "score",
                    "display_name": "Score",
                    "value_type": "number",
                }
            ],
            "points": [
                {
                    "canonical_key": "guard-point",
                    "display_name": "Guard point",
                    "dimensions": {},
                    "result_requirements": {
                        "required_metrics": ["score"],
                        "minimum_observations": 1,
                    },
                }
            ],
            "presentation": {"primary_metric": "score"},
        },
        request_id=f"publish-purge-guard-{token}",
    )
    study_id = str(published["study_id"])
    point = query_registry(
        paths,
        {
            "kind": "experiment_query",
            "schema_version": 1,
            "operation": "point_list",
            "study": {"study_id": study_id},
            "page": {"limit": 10, "cursor": None},
        },
    )["items"][0]
    binding = {
        "kind": "run_binding",
        "schema_version": 1,
        "binding_id": f"binding-{token}",
        "run_id": run_id,
        "source_revision": "a" * 40,
        "targets": [
            {
                "study_id": study_id,
                "origin_design_revision_id": published["design_revision_id"],
                "plan_digest": published["plan_digest"],
                "point_id": point["point_id"],
                "point_revision_id": point["point_revision_id"],
                "point_revision_digest": point["point_revision_digest"],
                "setting_digest": point["setting_digest"],
                "result_group_id": "guard-result",
                "contribution_role": "primary",
            }
        ],
        "expects_result_manifest": False,
    }
    normalized_binding = normalize_run_binding(binding)
    ingest_binding(paths, binding)
    result_id = f"result-{token}"
    ingested = ingest_result(
        paths,
        {
            "kind": "experiment_result",
            "schema_version": 1,
            "manifest_id": f"result-manifest-{token}",
            "emitter_run_id": run_id,
            "producer": {
                "name": "purge-guard-fixture",
                "version": "1",
                "mode": "legacy_adapter",
            },
            "results": [
                {
                    "result_id": result_id,
                    "study_id": study_id,
                    "origin_design_revision_id": published["design_revision_id"],
                    "plan_digest": published["plan_digest"],
                    "point_id": point["point_id"],
                    "point_revision_id": point["point_revision_id"],
                    "point_revision_digest": point["point_revision_digest"],
                    "setting_digest": point["setting_digest"],
                    "result_group_id": "guard-result",
                    "contributions": [
                        {
                            "run_id": run_id,
                            "binding_id": normalized_binding["binding_id"],
                            "binding_digest": normalized_binding["binding_digest"],
                            "role": "primary",
                        }
                    ],
                    "metrics": [{"key": "score", "value": 1.0}],
                    "evidence": {"observation_count": 1},
                    "artifacts": [],
                }
            ],
        },
    )
    return {"result_id": result_id, "ingested": ingested}
