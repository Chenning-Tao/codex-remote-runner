export type ExperimentPointStatus =
  | "complete"
  | "running"
  | "queued"
  | "review"
  | "failed"
  | "stale"
  | "planned";

export type ExperimentScalar = string | number | boolean | null;

export interface ExperimentMetric {
  key: string;
  label: string;
  shortLabel: string;
  unit?: string;
  format: "scientific" | "percent" | "integer" | "decimal" | "duration";
  scale: "linear" | "log";
  valueType?: "number" | "integer" | "string" | "boolean";
}

export interface MetricReading {
  value: ExperimentScalar;
  interval?: {
    lower: number;
    upper: number;
    level: number;
  };
}

export interface ExperimentRunRef {
  runId: string;
  role: "primary" | "continuation" | "replacement" | "aggregation";
  status: "queued" | "running" | "succeeded" | "failed" | "unknown";
}

export interface ExperimentResultCandidate {
  resultId: string;
  disposition: "accepted" | "superseded" | "review" | "retained";
  sourceRunIds: string[];
}

export interface ExperimentArtifactRef {
  label: string;
  uri: string;
}

export interface ExperimentPoint {
  pointId: string;
  pointRevisionId: string;
  canonicalKey: string;
  displayName: string;
  status: ExperimentPointStatus;
  dimensions: Record<string, ExperimentScalar>;
  metrics: Record<string, MetricReading>;
  historicalMetrics?: Record<string, MetricReading>;
  acceptedResultId?: string;
  observationCount?: number;
  candidateCount: number;
  staleReason?: string;
  failureReason?: string;
  settingDigest: string;
  pointRevisionDigest: string;
  changedComponents?: string[];
  resultHistory?: ExperimentResultCandidate[];
  artifacts?: ExperimentArtifactRef[];
  runs: ExperimentRunRef[];
}

export interface ExperimentStudy {
  studyId: string;
  canonicalKey: string;
  displayName: string;
  description: string;
  activeRevisionId: string;
  previousRevisionId?: string;
  planDigest: string;
  eventCursor: number;
  refreshedAt: string;
  dimensions: Record<string, string>;
  metrics: ExperimentMetric[];
  primaryMetric: string;
  presentation: {
    xDimension: string;
    seriesDimension: string;
    rowDimension: string;
    columnDimension: string;
  };
  impact: {
    unchanged: number;
    new: number;
    stale: number;
    archived: number;
  };
  points: ExperimentPoint[];
  pointCount?: number;
  statusCounts?: Record<ExperimentPointStatus, number>;
}

export const experimentDemoProject = {
  displayName: "decoder-atomloss-demo",
  registryEpoch: "demo-03",
};

function reading(
  value: number,
  lower?: number,
  upper?: number,
): MetricReading {
  return {
    value,
    ...(typeof lower === "number" && typeof upper === "number"
      ? { interval: { lower, upper, level: 0.95 } }
      : {}),
  };
}

function digest(seed: string): string {
  let state = 2166136261;
  let value = "";
  for (let index = 0; index < 64; index += 1) {
    const character = seed.charCodeAt(index % seed.length);
    state = Math.imul(state ^ character ^ index, 16777619);
    value += ((state >>> 28) & 0xf).toString(16);
  }
  return `sha256:${value}`;
}

function runId(seed: string): string {
  return `rr-${digest(seed).slice(7, 23)}`;
}

const lerPoints: ExperimentPoint[] = [
  {
    pointId: "point-41b0d3",
    pointRevisionId: "pointrev-108a31",
    canonicalKey: "sharingan-d3",
    displayName: "Sharingan / d=3",
    status: "complete",
    dimensions: { method: "Sharingan", distance: 3 },
    metrics: {
      per_round_ler: reading(0.0024, 0.00211, 0.00272),
      block_ler: reading(0.0237, 0.0218, 0.0257),
      logical_errors: reading(240),
      shots: reading(100000),
    },
    acceptedResultId: "result-a37e61",
    observationCount: 100000,
    candidateCount: 1,
    settingDigest: digest("31a8"),
    pointRevisionDigest: digest("c144"),
    resultHistory: [
      { resultId: "result-a37e61", disposition: "accepted", sourceRunIds: ["rr-14358a2f0d41e7b3"] },
    ],
    artifacts: [
      { label: "结果 manifest", uri: "runs/rr-14358a2f0d41e7b3/outputs/experiment-result.json" },
      { label: "执行 manifest", uri: "runs/rr-14358a2f0d41e7b3/run-manifest.yaml" },
    ],
    runs: [{ runId: "rr-14358a2f0d41e7b3", role: "primary", status: "succeeded" }],
  },
  {
    pointId: "point-580bfa",
    pointRevisionId: "pointrev-b261ec",
    canonicalKey: "sharingan-d5",
    displayName: "Sharingan / d=5",
    status: "complete",
    dimensions: { method: "Sharingan", distance: 5 },
    metrics: {
      per_round_ler: reading(0.00082, 0.00066, 0.00101),
      block_ler: reading(0.0081, 0.0067, 0.0097),
      logical_errors: reading(82),
      shots: reading(100000),
    },
    acceptedResultId: "result-93fb40",
    observationCount: 100000,
    candidateCount: 1,
    settingDigest: digest("31a8"),
    pointRevisionDigest: digest("32be"),
    runs: [{ runId: "rr-9859d2d7ca62784f", role: "primary", status: "succeeded" }],
  },
  {
    pointId: "point-52e476",
    pointRevisionId: "pointrev-d6156b",
    canonicalKey: "sharingan-d7",
    displayName: "Sharingan / d=7",
    status: "running",
    dimensions: { method: "Sharingan", distance: 7 },
    metrics: {},
    observationCount: 62000,
    candidateCount: 0,
    settingDigest: digest("31a8"),
    pointRevisionDigest: digest("d825"),
    runs: [{ runId: "rr-628387ad2d88837b", role: "primary", status: "running" }],
  },
  {
    pointId: "point-26cc1e",
    pointRevisionId: "pointrev-642a00",
    canonicalKey: "sharingan-d9",
    displayName: "Sharingan / d=9",
    status: "planned",
    dimensions: { method: "Sharingan", distance: 9 },
    metrics: {},
    candidateCount: 0,
    settingDigest: digest("31a8"),
    pointRevisionDigest: digest("184b"),
    runs: [],
  },
  {
    pointId: "point-e7f8d2",
    pointRevisionId: "pointrev-b53ea4",
    canonicalKey: "relay-d3",
    displayName: "Relay / d=3",
    status: "complete",
    dimensions: { method: "Relay", distance: 3 },
    metrics: {
      per_round_ler: reading(0.0031, 0.00277, 0.00347),
      block_ler: reading(0.0302, 0.0281, 0.0324),
      logical_errors: reading(310),
      shots: reading(100000),
    },
    acceptedResultId: "result-8faab1",
    observationCount: 100000,
    candidateCount: 1,
    settingDigest: digest("a5d0"),
    pointRevisionDigest: digest("6a02"),
    runs: [{ runId: "rr-8c118ddf811cfdda", role: "primary", status: "succeeded" }],
  },
  {
    pointId: "point-4ec389",
    pointRevisionId: "pointrev-853ac3",
    canonicalKey: "relay-d5",
    displayName: "Relay / d=5",
    status: "complete",
    dimensions: { method: "Relay", distance: 5 },
    metrics: {
      per_round_ler: reading(0.00118, 0.00097, 0.00142),
      block_ler: reading(0.0117, 0.0101, 0.0135),
      logical_errors: reading(118),
      shots: reading(100000),
    },
    acceptedResultId: "result-01967f",
    observationCount: 100000,
    candidateCount: 2,
    settingDigest: digest("a5d0"),
    pointRevisionDigest: digest("c9ee"),
    resultHistory: [
      { resultId: "result-01967f", disposition: "accepted", sourceRunIds: ["rr-e930cd493ef98738", "rr-0a30a4a38abbf215"] },
      { resultId: "result-5e22ad", disposition: "superseded", sourceRunIds: ["rr-e930cd493ef98738"] },
    ],
    artifacts: [
      { label: "结果 manifest", uri: "runs/rr-0a30a4a38abbf215/outputs/experiment-result.json" },
    ],
    runs: [
      { runId: "rr-e930cd493ef98738", role: "primary", status: "succeeded" },
      { runId: "rr-0a30a4a38abbf215", role: "continuation", status: "succeeded" },
    ],
  },
  {
    pointId: "point-ae63fe",
    pointRevisionId: "pointrev-c8790a",
    canonicalKey: "relay-d7",
    displayName: "Relay / d=7",
    status: "failed",
    dimensions: { method: "Relay", distance: 7 },
    metrics: {},
    candidateCount: 0,
    failureReason: "结果 manifest 缺少 required metric: per_round_ler",
    settingDigest: digest("a5d0"),
    pointRevisionDigest: digest("a8de"),
    runs: [{ runId: "rr-7f0501aa7357c5dc", role: "primary", status: "failed" }],
  },
  {
    pointId: "point-49ec80",
    pointRevisionId: "pointrev-2eaf64",
    canonicalKey: "relay-d9",
    displayName: "Relay / d=9",
    status: "stale",
    dimensions: { method: "Relay", distance: 9 },
    metrics: {},
    historicalMetrics: {
      per_round_ler: reading(0.00037, 0.00025, 0.00052),
      block_ler: reading(0.0037),
      logical_errors: reading(37),
      shots: reading(100000),
    },
    observationCount: 100000,
    candidateCount: 0,
    staleReason: "Resolution component digest changed",
    changedComponents: ["resolution"],
    settingDigest: digest("a5d1"),
    pointRevisionDigest: digest("7b62"),
    resultHistory: [
      { resultId: "result-retained-7d1e22", disposition: "retained", sourceRunIds: ["rr-cfb84af0322e13d1"] },
    ],
    artifacts: [
      { label: "历史结果 manifest", uri: "history/result-retained-7d1e22/experiment-result.json" },
    ],
    runs: [],
  },
  {
    pointId: "point-c9a24d",
    pointRevisionId: "pointrev-c22199",
    canonicalKey: "baseline-d3",
    displayName: "Baseline / d=3",
    status: "complete",
    dimensions: { method: "Baseline", distance: 3 },
    metrics: {
      per_round_ler: reading(0.0042, 0.00381, 0.00463),
      block_ler: reading(0.0407),
      logical_errors: reading(420),
      shots: reading(100000),
    },
    acceptedResultId: "result-476fef",
    observationCount: 100000,
    candidateCount: 1,
    settingDigest: digest("47c1"),
    pointRevisionDigest: digest("ad2e"),
    runs: [{ runId: "rr-a696715866932e82", role: "primary", status: "succeeded" }],
  },
  {
    pointId: "point-38d351",
    pointRevisionId: "pointrev-51f8f0",
    canonicalKey: "baseline-d5",
    displayName: "Baseline / d=5",
    status: "complete",
    dimensions: { method: "Baseline", distance: 5 },
    metrics: {
      per_round_ler: reading(0.00191, 0.00165, 0.00220),
      block_ler: reading(0.0189),
      logical_errors: reading(191),
      shots: reading(100000),
    },
    acceptedResultId: "result-f0daa5",
    observationCount: 100000,
    candidateCount: 1,
    settingDigest: digest("47c1"),
    pointRevisionDigest: digest("be17"),
    runs: [{ runId: "rr-0d400d7c08497723", role: "primary", status: "succeeded" }],
  },
  {
    pointId: "point-4200d7",
    pointRevisionId: "pointrev-f04ea1",
    canonicalKey: "baseline-d7",
    displayName: "Baseline / d=7",
    status: "complete",
    dimensions: { method: "Baseline", distance: 7 },
    metrics: {
      per_round_ler: reading(0.00091, 0.00074, 0.00111),
      block_ler: reading(0.0090),
      logical_errors: reading(91),
      shots: reading(100000),
    },
    acceptedResultId: "result-947dbd",
    observationCount: 100000,
    candidateCount: 1,
    settingDigest: digest("47c1"),
    pointRevisionDigest: digest("9d0c"),
    runs: [{ runId: "rr-58c37b3644c71e70", role: "primary", status: "succeeded" }],
  },
  {
    pointId: "point-00b913",
    pointRevisionId: "pointrev-3ca938",
    canonicalKey: "baseline-d9",
    displayName: "Baseline / d=9",
    status: "stale",
    dimensions: { method: "Baseline", distance: 9 },
    metrics: {},
    historicalMetrics: {
      per_round_ler: reading(0.00044, 0.00031, 0.00059),
      block_ler: reading(0.0044),
      logical_errors: reading(44),
      shots: reading(100000),
    },
    observationCount: 100000,
    candidateCount: 0,
    staleReason: "Resolution component digest changed",
    changedComponents: ["resolution"],
    settingDigest: digest("47c2"),
    pointRevisionDigest: digest("786d"),
    runs: [],
  },
];

const throughputPoints: ExperimentPoint[] = [
  ["Native", 1, "complete", 152, 18.4],
  ["Native", 4, "complete", 534, 24.1],
  ["Native", 16, "running", undefined, undefined],
  ["Native", 64, "planned", undefined, undefined],
  ["Fused", 1, "complete", 186, 16.7],
  ["Fused", 4, "complete", 668, 21.6],
  ["Fused", 16, "complete", 1814, 44.2],
  ["Fused", 64, "failed", undefined, undefined],
].map(([engine, batch, status, throughput, latency], index) => {
  const pointStatus = status as ExperimentPointStatus;
  const complete = pointStatus === "complete";
  const metrics: Record<string, MetricReading> = complete
    ? {
        samples_per_second: reading(Number(throughput)),
        p95_latency_ms: reading(Number(latency)),
      }
    : {};
  return {
    pointId: `point-throughput-${index + 1}`,
    pointRevisionId: `pointrev-throughput-${index + 1}`,
    canonicalKey: `${String(engine).toLowerCase()}-batch-${batch}`,
    displayName: `${engine} / batch=${batch}`,
    status: pointStatus,
    dimensions: { engine: String(engine), batch_size: Number(batch) },
    metrics,
    acceptedResultId: complete ? `result-throughput-${index + 1}` : undefined,
    observationCount: complete ? 500 : undefined,
    candidateCount: complete ? 1 : 0,
    failureReason: pointStatus === "failed" ? "运行在同步输出前失败" : undefined,
    settingDigest: digest(`t${index}`),
    pointRevisionDigest: digest(`p${index}`),
    runs:
      pointStatus === "running"
        ? [{ runId: "rr-40e67f346cc2cf39", role: "primary" as const, status: "running" as const }]
        : pointStatus === "failed"
          ? [{ runId: "rr-1c2651a1b9fa8af0", role: "primary" as const, status: "failed" as const }]
          : complete
            ? [{ runId: runId(`throughput-${index}`), role: "primary" as const, status: "succeeded" as const }]
            : [],
  };
});

const compilerPoints: ExperimentPoint[] = [
  ["clang", "O2", "complete", 42.7],
  ["clang", "O3", "review", 46.1],
  ["gcc", "O2", "complete", 51.8],
  ["gcc", "O3", "queued", undefined],
].map(([toolchain, level, status, duration], index) => {
  const pointStatus = status as ExperimentPointStatus;
  const hasMetric = typeof duration === "number";
  const metrics: Record<string, MetricReading> = hasMetric
    ? { build_seconds: reading(Number(duration)) }
    : {};
  return {
    pointId: `point-compiler-${index + 1}`,
    pointRevisionId: `pointrev-compiler-${index + 1}`,
    canonicalKey: `${toolchain}-${String(level).toLowerCase()}`,
    displayName: `${toolchain} / ${level}`,
    status: pointStatus,
    dimensions: { toolchain: String(toolchain), optimization: String(level) },
    metrics,
    acceptedResultId: pointStatus === "complete" ? `result-compiler-${index + 1}` : undefined,
    observationCount: hasMetric ? 20 : undefined,
    candidateCount: pointStatus === "review" ? 1 : pointStatus === "complete" ? 1 : 0,
    settingDigest: digest(`c${index}`),
    pointRevisionDigest: digest(`r${index}`),
    runs:
      pointStatus === "queued"
        ? [{ runId: "rr-af67bf25f699380a", role: "primary" as const, status: "queued" as const }]
        : hasMetric
          ? [{ runId: runId(`compiler-${index}`), role: "primary" as const, status: "succeeded" as const }]
          : [],
  };
});

export const experimentDemoStudies: ExperimentStudy[] = [
  {
    studyId: "study-59c18a",
    canonicalKey: "ler-main-sweep",
    displayName: "LER / Main sweep",
    description: "当前设计中的 decoder、distance 与 accepted LER 结果。",
    activeRevisionId: "design-8d19c4e8",
    previousRevisionId: "design-4a2b13dc",
    planDigest: digest("lerplan"),
    eventCursor: 1842,
    refreshedAt: "2026-07-26T10:42:18Z",
    dimensions: { method: "Method", distance: "Distance" },
    metrics: [
      { key: "per_round_ler", label: "Per-round LER", shortLabel: "LER / round", unit: "1/round", format: "scientific", scale: "log" },
      { key: "block_ler", label: "Block LER", shortLabel: "Block LER", format: "percent", scale: "log" },
      { key: "logical_errors", label: "Logical errors", shortLabel: "Errors", format: "integer", scale: "linear" },
      { key: "shots", label: "Shots", shortLabel: "Shots", format: "integer", scale: "linear" },
    ],
    primaryMetric: "per_round_ler",
    presentation: {
      xDimension: "distance",
      seriesDimension: "method",
      rowDimension: "method",
      columnDimension: "distance",
    },
    impact: { unchanged: 9, new: 1, stale: 2, archived: 1 },
    points: lerPoints,
  },
  {
    studyId: "study-77b226",
    canonicalKey: "throughput-batch-sweep",
    displayName: "Throughput / Batch sweep",
    description: "不同执行引擎与 batch size 的 accepted throughput。",
    activeRevisionId: "design-c91a778e",
    previousRevisionId: "design-bb908f42",
    planDigest: digest("throughputplan"),
    eventCursor: 1831,
    refreshedAt: "2026-07-26T10:39:02Z",
    dimensions: { engine: "Engine", batch_size: "Batch size" },
    metrics: [
      { key: "samples_per_second", label: "Samples per second", shortLabel: "samples/s", unit: "samples/s", format: "integer", scale: "linear" },
      { key: "p95_latency_ms", label: "P95 latency", shortLabel: "P95", unit: "ms", format: "decimal", scale: "linear" },
    ],
    primaryMetric: "samples_per_second",
    presentation: {
      xDimension: "batch_size",
      seriesDimension: "engine",
      rowDimension: "engine",
      columnDimension: "batch_size",
    },
    impact: { unchanged: 7, new: 1, stale: 0, archived: 0 },
    points: throughputPoints,
  },
  {
    studyId: "study-d83e11",
    canonicalKey: "compiler-optimization-matrix",
    displayName: "Compiler / Optimization matrix",
    description: "Toolchain 与 optimization level 的构建时间验证。",
    activeRevisionId: "design-5c4a03b7",
    previousRevisionId: "design-e13cf859",
    planDigest: digest("compilerplan"),
    eventCursor: 1816,
    refreshedAt: "2026-07-26T10:31:47Z",
    dimensions: { toolchain: "Toolchain", optimization: "Optimization" },
    metrics: [
      { key: "build_seconds", label: "Build duration", shortLabel: "Duration", unit: "s", format: "duration", scale: "linear" },
    ],
    primaryMetric: "build_seconds",
    presentation: {
      xDimension: "optimization",
      seriesDimension: "toolchain",
      rowDimension: "toolchain",
      columnDimension: "optimization",
    },
    impact: { unchanged: 4, new: 0, stale: 0, archived: 0 },
    points: compilerPoints,
  },
];

export function formatMetricValue(
  value: ExperimentScalar | undefined,
  metric: ExperimentMetric,
): string {
  if (value === null || typeof value === "undefined") return "--";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string") return value;
  switch (metric.format) {
    case "scientific":
      return value.toExponential(2);
    case "percent":
      return `${(value * 100).toFixed(value < 0.01 ? 3 : 1)}%`;
    case "integer":
      return Math.round(value).toLocaleString("zh-CN");
    case "duration":
      return `${value.toFixed(1)} s`;
    default:
      return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  }
}

export function pointStatusCounts(
  study: ExperimentStudy,
): Record<ExperimentPointStatus, number> {
  if (study.statusCounts) return { ...study.statusCounts };
  const counts: Record<ExperimentPointStatus, number> = {
    complete: 0,
    running: 0,
    queued: 0,
    review: 0,
    failed: 0,
    stale: 0,
    planned: 0,
  };
  for (const point of study.points) counts[point.status] += 1;
  return counts;
}
