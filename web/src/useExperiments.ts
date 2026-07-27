import { useCallback, useEffect, useState } from "react";
import type {
  ExperimentArtifactRef,
  ExperimentMetric,
  ExperimentPoint,
  ExperimentPointStatus,
  ExperimentResultCandidate,
  ExperimentRunRef,
  ExperimentScalar,
  ExperimentStudy,
  MetricReading,
} from "./experimentDemoData";

interface QueryResponse<T> {
  schema_version: number;
  project_id: string;
  registry_epoch: string;
  event_cursor: number;
  active_design_revision_id: string | null;
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

interface RawMetric {
  key: string;
  display_name: string;
  value_type: "number" | "integer" | "string" | "boolean";
  unit: string | null;
  default_format: string;
}

interface RawStudy {
  study_id: string;
  canonical_key: string;
  display_name: string;
  description: string;
  active_design_revision_id: string;
  previous_design_revision_id: string | null;
  plan_digest: string;
  dimensions: Array<{ key: string; display_name: string }>;
  metrics: RawMetric[];
  presentation: {
    primary_metric: string;
    curves: Array<{
      metric: string;
      x_dimension: string;
      series_dimensions: string[];
      scale: "linear" | "log";
    }>;
    matrix: {
      row_dimension: string;
      column_dimension: string;
    };
  };
  impact: { unchanged: number; new: number; stale: number; archived: number };
  status_counts: Record<ExperimentPointStatus, number>;
  point_count: number;
  event_cursor: number;
}

interface RawPoint {
  point_id: string;
  point_revision_id: string;
  canonical_key: string;
  display_name: string;
  dimensions: Record<string, ExperimentScalar>;
  status: ExperimentPointStatus;
  metrics: Record<string, MetricReading>;
  accepted_result_id: string | null;
  observation_count: number | null;
  candidate_count: number;
  stale_reason: string | null;
  setting_digest: string;
  point_revision_digest: string;
  runs: Array<{ run_id: string; role: string; status: string }>;
  result_history?: Array<{
    result_id: string;
    eligible: boolean;
    ineligibility_reasons: string[];
    observation_count: number;
  }>;
  artifacts?: Array<{
    role: string;
    run_id: string;
    relative_path: string;
  }>;
}

interface ExperimentsState {
  projectId: string | null;
  registryEpoch: string | null;
  eventCursor: number;
  studies: ExperimentStudy[];
  points: ExperimentPoint[];
  pointsStudyId: string | null;
  loadingStudies: boolean;
  loadingPoints: boolean;
  error: string | null;
}

const statuses: ExperimentPointStatus[] = [
  "complete",
  "running",
  "queued",
  "review",
  "failed",
  "stale",
  "planned",
];

function metricFormat(value: string): ExperimentMetric["format"] {
  if (value === "percentage" || value === "percent") return "percent";
  if (value === "scientific") return "scientific";
  if (value === "integer") return "integer";
  if (value === "duration") return "duration";
  return "decimal";
}

function statusCounts(raw: Record<string, number>): Record<ExperimentPointStatus, number> {
  return Object.fromEntries(statuses.map((status) => [status, raw[status] ?? 0])) as Record<
    ExperimentPointStatus,
    number
  >;
}

function adaptStudy(raw: RawStudy): ExperimentStudy {
  const dimensionKeys = raw.dimensions.map((dimension) => dimension.key);
  const curve = raw.presentation.curves[0];
  const rowDimension = raw.presentation.matrix.row_dimension || dimensionKeys[0] || "";
  const columnDimension = raw.presentation.matrix.column_dimension || dimensionKeys[1] || rowDimension;
  const xDimension = curve?.x_dimension || columnDimension;
  const seriesDimension = curve?.series_dimensions[0] || rowDimension;
  return {
    studyId: raw.study_id,
    canonicalKey: raw.canonical_key,
    displayName: raw.display_name,
    description: raw.description,
    activeRevisionId: raw.active_design_revision_id,
    previousRevisionId: raw.previous_design_revision_id ?? undefined,
    planDigest: raw.plan_digest,
    eventCursor: raw.event_cursor,
    refreshedAt: new Date().toISOString(),
    dimensions: Object.fromEntries(
      raw.dimensions.map((dimension) => [dimension.key, dimension.display_name]),
    ),
    metrics: raw.metrics.map((metric) => ({
      key: metric.key,
      label: metric.display_name,
      shortLabel: metric.display_name,
      unit: metric.unit ?? undefined,
      format: metricFormat(metric.default_format),
      scale: raw.presentation.curves.find((item) => item.metric === metric.key)?.scale ?? "linear",
      valueType: metric.value_type,
    })),
    primaryMetric: raw.presentation.primary_metric || raw.metrics[0]?.key || "",
    presentation: { xDimension, seriesDimension, rowDimension, columnDimension },
    impact: raw.impact,
    points: [],
    pointCount: raw.point_count,
    statusCounts: statusCounts(raw.status_counts),
  };
}

function runRole(value: string): ExperimentRunRef["role"] {
  return ["primary", "continuation", "replacement", "aggregation"].includes(value)
    ? value as ExperimentRunRef["role"]
    : "primary";
}

function runStatus(value: string): ExperimentRunRef["status"] {
  return ["queued", "running", "succeeded", "failed", "unknown"].includes(value)
    ? value as ExperimentRunRef["status"]
    : "unknown";
}

function adaptPoint(raw: RawPoint): ExperimentPoint {
  const runs = raw.runs.map((run) => ({
    runId: run.run_id,
    role: runRole(run.role),
    status: runStatus(run.status),
  }));
  const resultHistory: ExperimentResultCandidate[] | undefined = raw.result_history?.map((result) => ({
    resultId: result.result_id,
    disposition: result.result_id === raw.accepted_result_id
      ? "accepted"
      : result.eligible
        ? "review"
        : "retained",
    sourceRunIds: [],
  }));
  const artifacts: ExperimentArtifactRef[] | undefined = raw.artifacts?.map((artifact) => ({
    label: artifact.role,
    uri: `runs/${artifact.run_id}/${artifact.relative_path}`,
  }));
  return {
    pointId: raw.point_id,
    pointRevisionId: raw.point_revision_id,
    canonicalKey: raw.canonical_key,
    displayName: raw.display_name,
    status: raw.status,
    dimensions: raw.dimensions,
    metrics: raw.metrics,
    acceptedResultId: raw.accepted_result_id ?? undefined,
    observationCount: raw.observation_count ?? undefined,
    candidateCount: raw.candidate_count,
    staleReason: raw.stale_reason ?? undefined,
    settingDigest: raw.setting_digest,
    pointRevisionDigest: raw.point_revision_digest,
    resultHistory,
    artifacts,
    runs,
  };
}

async function postQuery<T>(payload: Record<string, unknown>): Promise<QueryResponse<T>> {
  const response = await fetch("/api/experiments/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    body: JSON.stringify({ kind: "experiment_query", schema_version: 1, ...payload }),
  });
  if (!response.ok) {
    let detail = `实验注册表请求失败（${response.status}）`;
    try {
      const error: unknown = await response.json();
      if (error && typeof error === "object" && "detail" in error && typeof error.detail === "string") {
        detail = error.detail;
      }
    } catch {
      // Keep the status fallback for a non-JSON proxy response.
    }
    throw new Error(detail);
  }
  return await response.json() as QueryResponse<T>;
}

async function allPages<T>(payload: Record<string, unknown>): Promise<QueryResponse<T>> {
  let cursor: string | null = null;
  let first: QueryResponse<T> | null = null;
  const items: T[] = [];
  for (let page = 0; page < 1000; page += 1) {
    const response: QueryResponse<T> = await postQuery<T>({
      ...payload,
      page: { limit: 200, cursor },
    });
    if (first === null) first = response;
    items.push(...response.items);
    cursor = response.next_cursor;
    if (!cursor) return { ...response, ...first, items, next_cursor: null, has_more: false };
  }
  throw new Error("实验注册表分页超过安全上限");
}

export function useExperiments(enabled: boolean, studyId: string | null) {
  const [generation, setGeneration] = useState(0);
  const [state, setState] = useState<ExperimentsState>({
    projectId: null,
    registryEpoch: null,
    eventCursor: 0,
    studies: [],
    points: [],
    pointsStudyId: null,
    loadingStudies: enabled,
    loadingPoints: false,
    error: null,
  });

  const refresh = useCallback(() => setGeneration((value) => value + 1), []);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setState((current) => ({ ...current, loadingStudies: true, error: null }));
    allPages<RawStudy>({ operation: "study_list" })
      .then((response) => {
        if (cancelled) return;
        setState((current) => ({
          ...current,
          projectId: response.project_id,
          registryEpoch: response.registry_epoch,
          eventCursor: response.event_cursor,
          studies: response.items.map(adaptStudy),
          loadingStudies: false,
        }));
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState((current) => ({
            ...current,
            loadingStudies: false,
            error: error instanceof Error ? error.message : "实验注册表请求失败",
          }));
        }
      });
    return () => { cancelled = true; };
  }, [enabled, generation]);

  useEffect(() => {
    if (!enabled || !studyId) {
      setState((current) => ({
        ...current,
        points: [],
        pointsStudyId: null,
        loadingPoints: false,
      }));
      return;
    }
    let cancelled = false;
    setState((current) => ({
      ...current,
      points: [],
      pointsStudyId: null,
      loadingPoints: true,
      error: null,
    }));
    allPages<RawPoint>({ operation: "point_list", study: { study_id: studyId } })
      .then((response) => {
        if (!cancelled) {
          setState((current) => ({
            ...current,
            eventCursor: response.event_cursor,
            points: response.items.map(adaptPoint),
            pointsStudyId: studyId,
            loadingPoints: false,
          }));
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState((current) => ({
            ...current,
            loadingPoints: false,
            error: error instanceof Error ? error.message : "实验点请求失败",
          }));
        }
      });
    return () => { cancelled = true; };
  }, [enabled, generation, studyId]);

  const loadPointDetail = useCallback(async (
    selectedStudyId: string,
    pointRevisionId: string,
  ): Promise<ExperimentPoint> => {
    const response = await postQuery<RawPoint>({
      operation: "point_detail",
      study: { study_id: selectedStudyId },
      point: { point_revision_id: pointRevisionId },
      page: { limit: 1, cursor: null },
    });
    if (!response.items[0]) throw new Error("实验点已经不在当前设计版本中");
    return adaptPoint(response.items[0]);
  }, []);

  return { ...state, refresh, loadPointDetail };
}
