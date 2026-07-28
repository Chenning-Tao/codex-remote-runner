from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from pathlib import PurePosixPath
from typing import AbstractSet, Any, Mapping, Sequence, cast


EXPERIMENT_SCHEMA_VERSION = 1
MAX_CONTRACT_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024
MAX_POINTS = 10_000
MAX_RESULTS = 256
MAX_METRICS = 128
MAX_ARTIFACTS = 128
MAX_QUERY_LIMIT = 500
DEFAULT_QUERY_LIMIT = 50

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^rr-[0-9a-f]{16}$")
GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
ID_RES = {
    prefix: re.compile(rf"^{re.escape(prefix)}-[0-9a-f]{{16,32}}$")
    for prefix in (
        "study",
        "design",
        "point",
        "pointrev",
        "binding",
        "result-manifest",
        "result",
        "acceptance",
        "experiment-event",
    )
}


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"contract is not canonical JSON: {exc}") from exc


def contract_digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def _document(value: object, kind: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{kind} must be a JSON object")
    document = dict(value)
    if len(canonical_json_bytes(document)) > MAX_CONTRACT_BYTES:
        raise ValueError(f"{kind} exceeds the {MAX_CONTRACT_BYTES}-byte limit")
    if document.get("kind") != kind:
        raise ValueError(f"contract kind must be {kind!r}")
    if document.get("schema_version") != EXPERIMENT_SCHEMA_VERSION:
        raise ValueError(f"unsupported {kind} schema_version")
    return document


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _list(value: object, field: str, *, maximum: int) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{field} must be a list")
    result = list(value)
    if len(result) > maximum:
        raise ValueError(f"{field} must contain at most {maximum} items")
    return result


def _keys(
    value: Mapping[str, Any],
    field: str,
    *,
    required: AbstractSet[str],
    optional: AbstractSet[str] = frozenset(),
) -> None:
    present = set(value)
    missing = sorted(required - present)
    unknown = sorted(present - required - optional)
    if missing:
        raise ValueError(f"{field} is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{field} contains unknown fields: {', '.join(unknown)}")


def _text(
    value: object,
    field: str,
    *,
    maximum: int = 512,
    empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ValueError(
            f"{field} must be a {'string' if empty else 'non-empty string'}"
        )
    if len(value) > maximum or "\x00" in value or "\r" in value:
        raise ValueError(f"{field} is invalid or exceeds {maximum} characters")
    return value


def _key(value: object, field: str) -> str:
    text = _text(value, field, maximum=128)
    if KEY_RE.fullmatch(text) is None:
        raise ValueError(f"{field} must be a stable key")
    return text


def _digest(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a sha256 digest")
    return value


def _id(
    value: object,
    prefix: str,
    field: str,
    *,
    optional: bool = False,
) -> str | None:
    if value is None and optional:
        return None
    pattern = ID_RES[prefix]
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field} must be an opaque {prefix} id")
    return value


def _metadata(value: object, field: str) -> dict[str, Any]:
    metadata = _object(value, field)
    if len(canonical_json_bytes(metadata)) > MAX_METADATA_BYTES:
        raise ValueError(f"{field} exceeds the {MAX_METADATA_BYTES}-byte limit")
    return metadata


def _aliases(value: object, field: str) -> list[str]:
    aliases = [
        _text(item, field, maximum=256) for item in _list(value, field, maximum=64)
    ]
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"{field} must not contain duplicates")
    return aliases


def _scalar(value: object, field: str) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"{field} must be a finite JSON scalar")


def _relative_path(value: object, field: str) -> str:
    text = _text(value, field, maximum=512)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or str(path) != text
        or text in {"", "."}
        or ".." in path.parts
    ):
        raise ValueError(f"{field} must be a normalized relative POSIX path")
    return text


def _normalized_digest(
    supplied: object,
    payload: object,
    field: str,
) -> str:
    computed = contract_digest(payload)
    if supplied is not None and _digest(supplied, field) != computed:
        raise ValueError(f"{field} does not match normalized content")
    return computed


def _dimension_value(
    value: object, value_type: str, field: str
) -> str | int | float | bool:
    normalized = _scalar(value, field)
    valid = {
        "string": isinstance(normalized, str),
        "integer": isinstance(normalized, int) and not isinstance(normalized, bool),
        "number": isinstance(normalized, (int, float))
        and not isinstance(normalized, bool),
        "boolean": isinstance(normalized, bool),
    }[value_type]
    if not valid:
        raise ValueError(f"{field} must have declared type {value_type}")
    assert normalized is not None
    return normalized


def normalize_experiment_plan(value: object) -> dict[str, Any]:
    raw = _document(value, "experiment_plan")
    _keys(
        raw,
        "experiment_plan",
        required={
            "kind",
            "schema_version",
            "study",
            "dimensions",
            "setting_components",
            "metrics",
            "points",
            "presentation",
        },
        optional={"expected_active_design_revision_id", "plan_digest"},
    )

    study = _object(raw["study"], "study")
    _keys(
        study,
        "study",
        required={"canonical_key", "display_name"},
        optional={"study_id", "aliases", "description", "metadata"},
    )
    normalized_study = {
        "study_id": _id(
            study.get("study_id"), "study", "study.study_id", optional=True
        ),
        "canonical_key": _key(study["canonical_key"], "study.canonical_key"),
        "display_name": _text(study["display_name"], "study.display_name", maximum=256),
        "aliases": _aliases(study.get("aliases", []), "study.aliases"),
        "description": _text(
            study.get("description", ""), "study.description", maximum=2048, empty=True
        ),
        "metadata": _metadata(study.get("metadata", {}), "study.metadata"),
    }

    dimensions: list[dict[str, Any]] = []
    dimension_types: dict[str, str] = {}
    for index, item in enumerate(_list(raw["dimensions"], "dimensions", maximum=64)):
        dimension = _object(item, f"dimensions[{index}]")
        _keys(
            dimension,
            f"dimensions[{index}]",
            required={"key", "display_name", "value_type"},
            optional={"order"},
        )
        key = _key(dimension["key"], f"dimensions[{index}].key")
        if key in dimension_types:
            raise ValueError(f"duplicate dimension key: {key}")
        value_type = _text(dimension["value_type"], f"dimensions[{index}].value_type")
        if value_type not in {"string", "integer", "number", "boolean"}:
            raise ValueError(f"unsupported dimension value_type: {value_type}")
        order = [
            _dimension_value(entry, value_type, f"dimensions[{index}].order")
            for entry in _list(
                dimension.get("order", []), f"dimensions[{index}].order", maximum=512
            )
        ]
        if len({canonical_json_bytes(entry) for entry in order}) != len(order):
            raise ValueError(f"dimensions[{index}].order must not contain duplicates")
        dimension_types[key] = value_type
        dimensions.append(
            {
                "key": key,
                "display_name": _text(
                    dimension["display_name"],
                    f"dimensions[{index}].display_name",
                    maximum=256,
                ),
                "value_type": value_type,
                "order": order,
            }
        )

    components: list[dict[str, Any]] = []
    component_digests: dict[str, str] = {}
    for index, item in enumerate(
        _list(raw["setting_components"], "setting_components", maximum=256)
    ):
        component = _object(item, f"setting_components[{index}]")
        _keys(
            component,
            f"setting_components[{index}]",
            required={"key", "digest"},
            optional={"metadata"},
        )
        key = _key(component["key"], f"setting_components[{index}].key")
        if key in component_digests:
            raise ValueError(f"duplicate setting component key: {key}")
        digest = _digest(component["digest"], f"setting_components[{index}].digest")
        assert digest is not None
        component_digests[key] = digest
        components.append(
            {
                "key": key,
                "digest": digest,
                "metadata": _metadata(
                    component.get("metadata", {}),
                    f"setting_components[{index}].metadata",
                ),
            }
        )

    metrics: list[dict[str, Any]] = []
    metric_keys: set[str] = set()
    for index, item in enumerate(_list(raw["metrics"], "metrics", maximum=MAX_METRICS)):
        metric = _object(item, f"metrics[{index}]")
        _keys(
            metric,
            f"metrics[{index}]",
            required={"key", "display_name", "value_type"},
            optional={"unit", "default_format"},
        )
        key = _key(metric["key"], f"metrics[{index}].key")
        if key in metric_keys:
            raise ValueError(f"duplicate metric key: {key}")
        value_type = _text(metric["value_type"], f"metrics[{index}].value_type")
        if value_type not in {"number", "integer", "string", "boolean"}:
            raise ValueError(f"unsupported metric value_type: {value_type}")
        metric_keys.add(key)
        metrics.append(
            {
                "key": key,
                "display_name": _text(
                    metric["display_name"],
                    f"metrics[{index}].display_name",
                    maximum=256,
                ),
                "value_type": value_type,
                "unit": None
                if metric.get("unit") is None
                else _text(metric["unit"], f"metrics[{index}].unit", maximum=64),
                "default_format": _text(
                    metric.get("default_format", "decimal"),
                    f"metrics[{index}].default_format",
                    maximum=32,
                ),
            }
        )

    points: list[dict[str, Any]] = []
    point_keys: set[str] = set()
    point_ids: set[str] = set()
    for index, item in enumerate(_list(raw["points"], "points", maximum=MAX_POINTS)):
        point = _object(item, f"points[{index}]")
        _keys(
            point,
            f"points[{index}]",
            required={"canonical_key", "display_name", "dimensions"},
            optional={
                "point_id",
                "reuse_point_revision_id",
                "aliases",
                "parameters",
                "setting_dependencies",
                "setting_digest",
                "result_requirements",
                "point_revision_digest",
                "metadata",
            },
        )
        canonical_key = _key(point["canonical_key"], f"points[{index}].canonical_key")
        if canonical_key in point_keys:
            raise ValueError(f"duplicate point canonical_key: {canonical_key}")
        point_keys.add(canonical_key)
        point_id = _id(
            point.get("point_id"), "point", f"points[{index}].point_id", optional=True
        )
        if point_id is not None:
            if point_id in point_ids:
                raise ValueError(f"duplicate point id: {point_id}")
            point_ids.add(point_id)
        dimensions_value = _object(point["dimensions"], f"points[{index}].dimensions")
        if set(dimensions_value) != set(dimension_types):
            raise ValueError(
                f"points[{index}].dimensions must exactly match the dimension catalog"
            )
        normalized_dimensions = {
            key: _dimension_value(
                dimensions_value[key], value_type, f"points[{index}].dimensions.{key}"
            )
            for key, value_type in dimension_types.items()
        }
        parameters = _object(point.get("parameters", {}), f"points[{index}].parameters")
        if len(canonical_json_bytes(parameters)) > MAX_METADATA_BYTES:
            raise ValueError(f"points[{index}].parameters exceeds the size limit")
        dependencies = [
            _key(entry, f"points[{index}].setting_dependencies")
            for entry in _list(
                point.get("setting_dependencies", []),
                f"points[{index}].setting_dependencies",
                maximum=256,
            )
        ]
        if len(set(dependencies)) != len(dependencies):
            raise ValueError(
                f"points[{index}].setting_dependencies must not contain duplicates"
            )
        unknown_dependencies = set(dependencies) - set(component_digests)
        if unknown_dependencies:
            raise ValueError(
                f"points[{index}] references unknown setting components: {', '.join(sorted(unknown_dependencies))}"
            )
        setting_payload = {
            "parameters": parameters,
            "components": [
                {"key": key, "digest": component_digests[key]}
                for key in sorted(dependencies)
            ],
        }
        setting_digest = _normalized_digest(
            point.get("setting_digest"),
            setting_payload,
            f"points[{index}].setting_digest",
        )
        requirements = _object(
            point.get("result_requirements", {}), f"points[{index}].result_requirements"
        )
        _keys(
            requirements,
            f"points[{index}].result_requirements",
            required=set(),
            optional={
                "required_metrics",
                "minimum_observations",
                "required_artifact_roles",
                "required_checks",
            },
        )
        required_metrics = [
            _key(entry, f"points[{index}].result_requirements.required_metrics")
            for entry in _list(
                requirements.get("required_metrics", []),
                f"points[{index}].result_requirements.required_metrics",
                maximum=MAX_METRICS,
            )
        ]
        if set(required_metrics) - metric_keys:
            raise ValueError(f"points[{index}] requires unknown metrics")
        minimum_observations = requirements.get("minimum_observations", 0)
        if (
            isinstance(minimum_observations, bool)
            or not isinstance(minimum_observations, int)
            or minimum_observations < 0
        ):
            raise ValueError(
                f"points[{index}].result_requirements.minimum_observations must be non-negative"
            )
        normalized_requirements = {
            "required_metrics": required_metrics,
            "minimum_observations": minimum_observations,
            "required_artifact_roles": [
                _key(
                    entry,
                    f"points[{index}].result_requirements.required_artifact_roles",
                )
                for entry in _list(
                    requirements.get("required_artifact_roles", []),
                    f"points[{index}].result_requirements.required_artifact_roles",
                    maximum=MAX_ARTIFACTS,
                )
            ],
            "required_checks": [
                _key(entry, f"points[{index}].result_requirements.required_checks")
                for entry in _list(
                    requirements.get("required_checks", []),
                    f"points[{index}].result_requirements.required_checks",
                    maximum=128,
                )
            ],
        }
        revision_payload = {
            "dimensions": normalized_dimensions,
            "parameters": parameters,
            "setting_digest": setting_digest,
            "result_requirements": normalized_requirements,
        }
        revision_digest = _normalized_digest(
            point.get("point_revision_digest"),
            revision_payload,
            f"points[{index}].point_revision_digest",
        )
        points.append(
            {
                "point_id": point_id,
                "reuse_point_revision_id": _id(
                    point.get("reuse_point_revision_id"),
                    "pointrev",
                    f"points[{index}].reuse_point_revision_id",
                    optional=True,
                ),
                "canonical_key": canonical_key,
                "display_name": _text(
                    point["display_name"], f"points[{index}].display_name", maximum=256
                ),
                "aliases": _aliases(
                    point.get("aliases", []), f"points[{index}].aliases"
                ),
                "dimensions": normalized_dimensions,
                "parameters": parameters,
                "setting_dependencies": dependencies,
                "setting_digest": setting_digest,
                "result_requirements": normalized_requirements,
                "point_revision_digest": revision_digest,
                "metadata": _metadata(
                    point.get("metadata", {}), f"points[{index}].metadata"
                ),
            }
        )

    presentation = _object(raw["presentation"], "presentation")
    _keys(
        presentation,
        "presentation",
        required={"primary_metric"},
        optional={"results", "curves", "matrix"},
    )
    primary_metric = _key(presentation["primary_metric"], "presentation.primary_metric")
    if primary_metric not in metric_keys:
        raise ValueError("presentation.primary_metric is not declared")
    normalized_presentation = deepcopy(presentation)
    normalized_presentation["primary_metric"] = primary_metric
    if len(canonical_json_bytes(normalized_presentation)) > MAX_METADATA_BYTES:
        raise ValueError("presentation exceeds the size limit")

    normalized = {
        "kind": "experiment_plan",
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "study": normalized_study,
        "expected_active_design_revision_id": _id(
            raw.get("expected_active_design_revision_id"),
            "design",
            "expected_active_design_revision_id",
            optional=True,
        ),
        "dimensions": dimensions,
        "setting_components": components,
        "metrics": metrics,
        "points": points,
        "presentation": normalized_presentation,
    }
    digest_payload = deepcopy(normalized)
    digest_payload.pop("expected_active_design_revision_id")
    digest_payload["study"].pop("study_id")
    for point in digest_payload["points"]:
        point.pop("point_id")
        point.pop("reuse_point_revision_id")
    normalized["plan_digest"] = _normalized_digest(
        raw.get("plan_digest"), digest_payload, "plan_digest"
    )
    return normalized


def normalize_run_binding(value: object) -> dict[str, Any]:
    raw = _document(value, "run_binding")
    _keys(
        raw,
        "run_binding",
        required={
            "kind",
            "schema_version",
            "binding_id",
            "run_id",
            "source_revision",
            "targets",
            "expects_result_manifest",
        },
        optional={"result_manifest_relpath", "metadata", "binding_digest"},
    )
    binding_id = _id(raw["binding_id"], "binding", "binding_id")
    assert binding_id is not None
    run_id = _text(raw["run_id"], "run_id")
    if RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_binding run_id is invalid")
    revision = _text(raw["source_revision"], "source_revision")
    if GIT_REVISION_RE.fullmatch(revision) is None:
        raise ValueError("run_binding source_revision must be a full Git SHA")
    targets: list[dict[str, Any]] = []
    for index, item in enumerate(_list(raw["targets"], "targets", maximum=256)):
        target = _object(item, f"targets[{index}]")
        _keys(
            target,
            f"targets[{index}]",
            required={
                "study_id",
                "origin_design_revision_id",
                "plan_digest",
                "point_id",
                "point_revision_id",
                "point_revision_digest",
                "setting_digest",
                "result_group_id",
                "contribution_role",
            },
        )
        role = _text(target["contribution_role"], f"targets[{index}].contribution_role")
        if role not in {"primary", "continuation", "replacement", "aggregation"}:
            raise ValueError(f"targets[{index}].contribution_role is invalid")
        targets.append(
            {
                "study_id": _id(
                    target["study_id"], "study", f"targets[{index}].study_id"
                ),
                "origin_design_revision_id": _id(
                    target["origin_design_revision_id"],
                    "design",
                    f"targets[{index}].origin_design_revision_id",
                ),
                "plan_digest": _digest(
                    target["plan_digest"], f"targets[{index}].plan_digest"
                ),
                "point_id": _id(
                    target["point_id"], "point", f"targets[{index}].point_id"
                ),
                "point_revision_id": _id(
                    target["point_revision_id"],
                    "pointrev",
                    f"targets[{index}].point_revision_id",
                ),
                "point_revision_digest": _digest(
                    target["point_revision_digest"],
                    f"targets[{index}].point_revision_digest",
                ),
                "setting_digest": _digest(
                    target["setting_digest"], f"targets[{index}].setting_digest"
                ),
                "result_group_id": _key(
                    target["result_group_id"], f"targets[{index}].result_group_id"
                ),
                "contribution_role": role,
            }
        )
    if not targets:
        raise ValueError("run_binding targets must not be empty")
    expects = raw["expects_result_manifest"]
    if not isinstance(expects, bool):
        raise ValueError("expects_result_manifest must be boolean")
    relpath = raw.get("result_manifest_relpath")
    if expects and relpath is None:
        raise ValueError(
            "result_manifest_relpath is required when a result manifest is expected"
        )
    normalized = {
        "kind": "run_binding",
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "binding_id": binding_id,
        "run_id": run_id,
        "source_revision": revision,
        "targets": targets,
        "result_manifest_relpath": None
        if relpath is None
        else _relative_path(relpath, "result_manifest_relpath"),
        "expects_result_manifest": expects,
        "metadata": _metadata(raw.get("metadata", {}), "metadata"),
    }
    normalized["binding_digest"] = _normalized_digest(
        raw.get("binding_digest"), normalized, "binding_digest"
    )
    return normalized


def normalize_experiment_result(value: object) -> dict[str, Any]:
    raw = _document(value, "experiment_result")
    _keys(
        raw,
        "experiment_result",
        required={
            "kind",
            "schema_version",
            "manifest_id",
            "emitter_run_id",
            "producer",
            "results",
        },
        optional={"manifest_digest"},
    )
    manifest_id = _id(raw["manifest_id"], "result-manifest", "manifest_id")
    emitter_run_id = _text(raw["emitter_run_id"], "emitter_run_id")
    if RUN_ID_RE.fullmatch(emitter_run_id) is None:
        raise ValueError("emitter_run_id is invalid")
    producer = _object(raw["producer"], "producer")
    _keys(producer, "producer", required={"name", "version", "mode"})
    mode = _text(producer["mode"], "producer.mode")
    if mode not in {"native", "legacy_adapter"}:
        raise ValueError("producer.mode must be native or legacy_adapter")
    normalized_results: list[dict[str, Any]] = []
    result_ids: set[str] = set()
    for index, item in enumerate(_list(raw["results"], "results", maximum=MAX_RESULTS)):
        result = _object(item, f"results[{index}]")
        _keys(
            result,
            f"results[{index}]",
            required={
                "result_id",
                "study_id",
                "origin_design_revision_id",
                "plan_digest",
                "point_id",
                "point_revision_id",
                "point_revision_digest",
                "setting_digest",
                "result_group_id",
                "contributions",
                "metrics",
                "evidence",
                "artifacts",
            },
            optional={"metadata"},
        )
        result_id = _id(result["result_id"], "result", f"results[{index}].result_id")
        assert result_id is not None
        if result_id in result_ids:
            raise ValueError(f"duplicate result id: {result_id}")
        result_ids.add(result_id)
        contributions: list[dict[str, Any]] = []
        for contribution_index, entry in enumerate(
            _list(
                result["contributions"], f"results[{index}].contributions", maximum=256
            )
        ):
            contribution = _object(
                entry, f"results[{index}].contributions[{contribution_index}]"
            )
            _keys(
                contribution,
                "contribution",
                required={"run_id", "binding_id", "binding_digest", "role"},
                optional={"replaces_run_id"},
            )
            contribution_run_id = _text(contribution["run_id"], "contribution.run_id")
            if RUN_ID_RE.fullmatch(contribution_run_id) is None:
                raise ValueError("contribution run_id is invalid")
            role = _text(contribution["role"], "contribution.role")
            if role not in {"primary", "continuation", "replacement", "aggregation"}:
                raise ValueError("contribution role is invalid")
            replaces = contribution.get("replaces_run_id")
            if replaces is not None and (
                not isinstance(replaces, str) or RUN_ID_RE.fullmatch(replaces) is None
            ):
                raise ValueError("contribution replaces_run_id is invalid")
            contributions.append(
                {
                    "run_id": contribution_run_id,
                    "binding_id": _id(
                        contribution["binding_id"], "binding", "contribution.binding_id"
                    ),
                    "binding_digest": _digest(
                        contribution["binding_digest"], "contribution.binding_digest"
                    ),
                    "role": role,
                    "replaces_run_id": replaces,
                }
            )
        if not contributions:
            raise ValueError(f"results[{index}].contributions must not be empty")
        normalized_metrics: list[dict[str, Any]] = []
        seen_metrics: set[str] = set()
        for metric_index, entry in enumerate(
            _list(result["metrics"], f"results[{index}].metrics", maximum=MAX_METRICS)
        ):
            metric = _object(entry, f"results[{index}].metrics[{metric_index}]")
            _keys(
                metric,
                "result metric",
                required={"key", "value"},
                optional={"interval"},
            )
            key = _key(metric["key"], "result metric key")
            if key in seen_metrics:
                raise ValueError(f"duplicate result metric key: {key}")
            seen_metrics.add(key)
            value_scalar = _scalar(metric["value"], "result metric value")
            interval = metric.get("interval")
            normalized_interval = None
            if interval is not None:
                interval_value = _object(interval, "result metric interval")
                _keys(
                    interval_value,
                    "result metric interval",
                    required={"lower", "upper", "level", "method"},
                )
                lower = _scalar(interval_value["lower"], "interval.lower")
                upper = _scalar(interval_value["upper"], "interval.upper")
                level = _scalar(interval_value["level"], "interval.level")
                if not all(
                    isinstance(item, (int, float)) and not isinstance(item, bool)
                    for item in (lower, upper, level)
                ):
                    raise ValueError("metric interval values must be numeric")
                numeric_lower = cast(int | float, lower)
                numeric_upper = cast(int | float, upper)
                numeric_level = cast(int | float, level)
                if (
                    float(numeric_lower) > float(numeric_upper)
                    or not 0 < float(numeric_level) <= 1
                ):
                    raise ValueError("metric interval bounds or level are invalid")
                normalized_interval = {
                    "lower": lower,
                    "upper": upper,
                    "level": level,
                    "method": _text(
                        interval_value["method"], "interval.method", maximum=128
                    ),
                }
            normalized_metrics.append(
                {"key": key, "value": value_scalar, "interval": normalized_interval}
            )
        evidence = _object(result["evidence"], f"results[{index}].evidence")
        _keys(evidence, "evidence", required={"observation_count"}, optional={"checks"})
        observation_count = evidence["observation_count"]
        if (
            isinstance(observation_count, bool)
            or not isinstance(observation_count, int)
            or observation_count < 0
        ):
            raise ValueError("evidence.observation_count must be non-negative")
        artifacts: list[dict[str, Any]] = []
        for artifact_index, entry in enumerate(
            _list(
                result["artifacts"],
                f"results[{index}].artifacts",
                maximum=MAX_ARTIFACTS,
            )
        ):
            artifact = _object(entry, f"results[{index}].artifacts[{artifact_index}]")
            _keys(
                artifact,
                "result artifact",
                required={"run_id", "role", "relative_path", "sha256", "media_type"},
                optional={"size"},
            )
            artifact_run_id = _text(artifact["run_id"], "artifact.run_id")
            if RUN_ID_RE.fullmatch(artifact_run_id) is None:
                raise ValueError("artifact run_id is invalid")
            size = artifact.get("size")
            if size is not None and (
                isinstance(size, bool) or not isinstance(size, int) or size < 0
            ):
                raise ValueError("artifact size must be non-negative")
            artifacts.append(
                {
                    "run_id": artifact_run_id,
                    "role": _key(artifact["role"], "artifact.role"),
                    "relative_path": _relative_path(
                        artifact["relative_path"], "artifact.relative_path"
                    ),
                    "sha256": _digest(artifact["sha256"], "artifact.sha256"),
                    "media_type": _text(
                        artifact["media_type"], "artifact.media_type", maximum=128
                    ),
                    "size": size,
                }
            )
        normalized_results.append(
            {
                "result_id": result_id,
                "study_id": _id(result["study_id"], "study", "result.study_id"),
                "origin_design_revision_id": _id(
                    result["origin_design_revision_id"],
                    "design",
                    "result.origin_design_revision_id",
                ),
                "plan_digest": _digest(result["plan_digest"], "result.plan_digest"),
                "point_id": _id(result["point_id"], "point", "result.point_id"),
                "point_revision_id": _id(
                    result["point_revision_id"], "pointrev", "result.point_revision_id"
                ),
                "point_revision_digest": _digest(
                    result["point_revision_digest"], "result.point_revision_digest"
                ),
                "setting_digest": _digest(
                    result["setting_digest"], "result.setting_digest"
                ),
                "result_group_id": _key(
                    result["result_group_id"], "result.result_group_id"
                ),
                "contributions": contributions,
                "metrics": normalized_metrics,
                "evidence": {
                    "observation_count": observation_count,
                    "checks": [
                        _key(entry, "evidence.checks")
                        for entry in _list(
                            evidence.get("checks", []), "evidence.checks", maximum=128
                        )
                    ],
                },
                "artifacts": artifacts,
                "metadata": _metadata(result.get("metadata", {}), "result.metadata"),
            }
        )
    if not normalized_results:
        raise ValueError("experiment_result results must not be empty")
    normalized = {
        "kind": "experiment_result",
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "manifest_id": manifest_id,
        "emitter_run_id": emitter_run_id,
        "producer": {
            "name": _text(producer["name"], "producer.name", maximum=128),
            "version": _text(producer["version"], "producer.version", maximum=128),
            "mode": mode,
        },
        "results": normalized_results,
    }
    normalized["manifest_digest"] = _normalized_digest(
        raw.get("manifest_digest"), normalized, "manifest_digest"
    )
    return normalized


def normalize_experiment_query(value: object) -> dict[str, Any]:
    raw = _document(value, "experiment_query")
    _keys(
        raw,
        "experiment_query",
        required={"kind", "schema_version", "operation"},
        optional={
            "study",
            "point",
            "revision_scope",
            "filters",
            "fields",
            "changed_since",
            "page",
        },
    )
    operation = _text(raw["operation"], "operation")
    if operation not in {
        "dashboard",
        "study_list",
        "study_status",
        "point_list",
        "point_detail",
        "point_history",
        "rerun_list",
    }:
        raise ValueError(f"unsupported experiment query operation: {operation}")
    study = _object(raw.get("study", {}), "study")
    _keys(study, "study", required=set(), optional={"study_id", "canonical_key"})
    study_id = _id(study.get("study_id"), "study", "study.study_id", optional=True)
    canonical_key = (
        None
        if study.get("canonical_key") is None
        else _key(study["canonical_key"], "study.canonical_key")
    )
    if study_id is not None and canonical_key is not None:
        raise ValueError("study query may use study_id or canonical_key, not both")
    if operation not in {"study_list"} and study_id is None and canonical_key is None:
        raise ValueError(f"{operation} requires a study selector")
    point = _object(raw.get("point", {}), "point")
    _keys(
        point,
        "point",
        required=set(),
        optional={"point_id", "point_revision_id", "canonical_key"},
    )
    point_id = _id(point.get("point_id"), "point", "point.point_id", optional=True)
    point_revision_id = _id(
        point.get("point_revision_id"),
        "pointrev",
        "point.point_revision_id",
        optional=True,
    )
    point_key = (
        None
        if point.get("canonical_key") is None
        else _key(point["canonical_key"], "point.canonical_key")
    )
    if (
        operation in {"point_detail", "point_history"}
        and sum(item is not None for item in (point_id, point_revision_id, point_key))
        != 1
    ):
        raise ValueError(f"{operation} requires exactly one point selector")
    filters = _object(raw.get("filters", {}), "filters")
    _keys(
        filters,
        "filters",
        required=set(),
        optional={"status", "dimensions", "canonical_key_prefix"},
    )
    statuses = [
        _text(item, "filters.status", maximum=32)
        for item in _list(filters.get("status", []), "filters.status", maximum=16)
    ]
    allowed_statuses = {
        "complete",
        "running",
        "queued",
        "review",
        "failed",
        "stale",
        "planned",
        "archived",
    }
    if set(statuses) - allowed_statuses:
        raise ValueError("filters.status contains an unsupported status")
    dimensions_filter = _object(filters.get("dimensions", {}), "filters.dimensions")
    normalized_dimensions_filter = {
        _key(key, "filters.dimensions key"): [
            _scalar(entry, f"filters.dimensions.{key}")
            for entry in _list(entries, f"filters.dimensions.{key}", maximum=256)
        ]
        for key, entries in dimensions_filter.items()
    }
    fields = [
        _key(item, "fields")
        for item in _list(raw.get("fields", []), "fields", maximum=64)
    ]
    if len(set(fields)) != len(fields):
        raise ValueError("fields must not contain duplicates")
    revision_scope = _object(
        raw.get("revision_scope", {"active": True}), "revision_scope"
    )
    _keys(revision_scope, "revision_scope", required={"active"})
    if revision_scope["active"] is not True:
        raise ValueError(
            "only the active experiment revision scope is currently supported"
        )
    changed_since = raw.get("changed_since")
    if changed_since is not None and (
        isinstance(changed_since, bool)
        or not isinstance(changed_since, int)
        or changed_since < 0
    ):
        raise ValueError("changed_since must be a non-negative event cursor")
    page = _object(raw.get("page", {}), "page")
    _keys(page, "page", required=set(), optional={"limit", "cursor"})
    limit = page.get("limit", DEFAULT_QUERY_LIMIT)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_QUERY_LIMIT
    ):
        raise ValueError(f"page.limit must be between 1 and {MAX_QUERY_LIMIT}")
    cursor = page.get("cursor")
    if cursor is not None:
        cursor = _text(cursor, "page.cursor", maximum=2048)
    return {
        "kind": "experiment_query",
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "operation": operation,
        "study": {"study_id": study_id, "canonical_key": canonical_key},
        "point": {
            "point_id": point_id,
            "point_revision_id": point_revision_id,
            "canonical_key": point_key,
        },
        "revision_scope": revision_scope,
        "filters": {
            "status": statuses,
            "dimensions": normalized_dimensions_filter,
            "canonical_key_prefix": None
            if filters.get("canonical_key_prefix") is None
            else _key(filters["canonical_key_prefix"], "filters.canonical_key_prefix"),
        },
        "fields": fields,
        "changed_since": changed_since,
        "page": {"limit": limit, "cursor": cursor},
    }


def normalize_acceptance_request(value: object) -> dict[str, Any]:
    raw = _object(value, "acceptance request")
    _keys(
        raw,
        "acceptance request",
        required={
            "point_revision_id",
            "result_id",
            "action",
            "actor",
            "reason",
            "policy",
        },
        optional={
            "acceptance_id",
            "expected_current_acceptance_id",
            "supersedes_acceptance_id",
        },
    )
    action = _text(raw["action"], "action")
    if action not in {"accept", "reject", "revoke", "supersede"}:
        raise ValueError("acceptance action is invalid")
    return {
        "acceptance_id": _id(
            raw.get("acceptance_id"), "acceptance", "acceptance_id", optional=True
        ),
        "point_revision_id": _id(
            raw["point_revision_id"], "pointrev", "point_revision_id"
        ),
        "result_id": _id(raw["result_id"], "result", "result_id"),
        "expected_current_acceptance_id": _id(
            raw.get("expected_current_acceptance_id"),
            "acceptance",
            "expected_current_acceptance_id",
            optional=True,
        ),
        "action": action,
        "actor": _text(raw["actor"], "actor", maximum=256),
        "reason": _text(raw["reason"], "reason", maximum=2048),
        "policy": _key(raw["policy"], "policy"),
        "supersedes_acceptance_id": _id(
            raw.get("supersedes_acceptance_id"),
            "acceptance",
            "supersedes_acceptance_id",
            optional=True,
        ),
    }
