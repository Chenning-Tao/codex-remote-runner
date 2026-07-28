import canonicalSnapshot from "./decoderCanonicalSnapshot.json";

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
  disposition: "accepted" | "rejected" | "superseded" | "review" | "retained";
  sourceRunIds: string[];
  observationCount?: number;
  metrics?: Record<string, MetricReading>;
  ineligibilityReasons?: string[];
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
  acceptedAcceptanceId?: string;
  acceptedResultId?: string;
  sourceRecordId?: string;
  observationCount?: number;
  candidateCount: number;
  staleReason?: string;
  failureReason?: string;
  reviewReason?: string;
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
  mode?: "registry" | "project-snapshot";
  activeRevisionId: string;
  previousRevisionId?: string;
  planDigest: string;
  eventCursor: number;
  refreshedAt: string;
  dimensions: Record<string, string>;
  dimensionOptions?: Record<string, ExperimentScalar[]>;
  metrics: ExperimentMetric[];
  primaryMetric: string;
  presentation: {
    xDimension: string;
    seriesDimension: string;
    rowDimension: string;
    columnDimension: string;
    facetDimensions?: string[];
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
  displayName: "decoder_atomloss",
  registryEpoch: "snapshot-20260727",
  snapshotAt: "2026-07-27T05:10:17.593152+00:00",
  snapshotSequence: 202,
};

function reading(value: number): MetricReading {
  return { value };
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

function pointIds(prefix: string, key: string) {
  const slug = key.replaceAll(/[^a-zA-Z0-9]+/g, "-").replaceAll(/^-|-$/g, "");
  return {
    pointId: `point-${prefix}-${slug}`,
    pointRevisionId: `snapshot-${prefix}-${slug}`,
  };
}

const emptyStatusCounts = (): Record<ExperimentPointStatus, number> => ({
  complete: 0,
  running: 0,
  queued: 0,
  review: 0,
  failed: 0,
  stale: 0,
  planned: 0,
});

type CanonicalLerRow = [
  codeLabel: string,
  method: string,
  pTotal: number,
  shots: number,
  logicalErrors: number,
  ler: number,
  lerPerRound: number,
  convergenceRate: number,
  wallTime: number,
  perShotCount: number | null,
];

const canonicalLerRows = canonicalSnapshot.rows as CanonicalLerRow[];

const canonicalLerPoints: ExperimentPoint[] = canonicalLerRows.map(([
  codeLabel,
  method,
  pTotal,
  shots,
  logicalErrors,
  ler,
  lerPerRound,
  convergenceRate,
  wallTime,
  perShotCount,
]) => {
  const key = `${codeLabel}|${method}|${pTotal.toFixed(3)}`;
  const needsReview = perShotCount !== null;
  return {
    ...pointIds("canonical", key),
    canonicalKey: key,
    displayName: `${codeLabel} / ${method} / p=${pTotal.toFixed(3)}`,
    status: needsReview ? "review" : "complete",
    dimensions: { code_label: codeLabel, method, p_total: pTotal },
    metrics: {
      ler: reading(ler),
      ler_per_round: reading(lerPerRound),
      convergence_rate: reading(convergenceRate),
      logical_errors: reading(logicalErrors),
      shots: reading(shots),
      wall_time: reading(wallTime),
    },
    sourceRecordId: `eval-sweep:${codeLabel}:${method}:p${pTotal.toFixed(3)}`,
    observationCount: shots,
    candidateCount: 1,
    reviewReason: needsReview
      ? `strict-convergence audit 标记 legacy per-shot 不完整：${perShotCount.toLocaleString("en-US")}/${shots.toLocaleString("en-US")} 条。`
      : undefined,
    settingDigest: digest(`canonical-setting:${key}`),
    pointRevisionDigest: digest(`canonical-row:${key}:b30d973e`),
    artifacts: [
      { label: "Canonical CSV", uri: "exp/eval/main-sweep/results/eval_sweep_combined.csv" },
      { label: "Strict convergence audit", uri: "exp/eval/main-sweep/results/strict_convergence_gap_report.csv" },
    ],
    runs: [],
  };
});

interface RelayRunningRow {
  method: string;
  pTotal: number;
  runId: string;
  server: string;
  progress: number;
  shots: number;
  logicalErrors: number;
}

const relayRunningRows: RelayRunningRow[] = [
  { method: "plain_pair_blind_legacy", pTotal: 0.0035, runId: "rr-172bcf3307f4259d", server: "TCLOUD71", progress: 0.0078, shots: 156, logicalErrors: 0 },
  { method: "resolution", pTotal: 0.0035, runId: "rr-72f37056ec91ed50", server: "8802", progress: 0, shots: 60000, logicalErrors: 47 },
  { method: "bare", pTotal: 0.004, runId: "rr-a9cbb819f0295f4b", server: "3090", progress: 0, shots: 0, logicalErrors: 0 },
  { method: "plain_pair_blind_legacy", pTotal: 0.004, runId: "rr-6b277ae6d9b635fb", server: "TCLOUD206", progress: 0, shots: 0, logicalErrors: 0 },
  { method: "gu_marginal_conversion", pTotal: 0.004, runId: "rr-e172e40f4c261814", server: "H100", progress: 0, shots: 0, logicalErrors: 0 },
];

const relayQueuedRows: Array<[method: string, pTotal: number, runId: string]> = [
  ["perrin_accurate_current_dem", 0.004, "rr-01590253e18405eb"],
  ["resolution", 0.004, "rr-0fe27a3bb4d5d801"],
  ["bare", 0.0045, "rr-69ab03ed551b1f6d"],
  ["plain_pair_blind_legacy", 0.0045, "rr-963d8d1a53ef0175"],
  ["gu_marginal_conversion", 0.0045, "rr-1c52970f9d0e08f8"],
  ["perrin_accurate_current_dem", 0.0045, "rr-747123f69d39b61a"],
  ["resolution", 0.0045, "rr-39a97801d50a8d64"],
  ["bare", 0.005, "rr-5e16d6f676fb8fd1"],
  ["plain_pair_blind_legacy", 0.005, "rr-64039618ddb07330"],
  ["gu_marginal_conversion", 0.005, "rr-cd2529f7f26ee778"],
  ["perrin_accurate_current_dem", 0.005, "rr-f4546b4c6878552a"],
  ["resolution", 0.005, "rr-1586259c8d051466"],
  ["bare", 0.0055, "rr-0c418afbdcd1a2f8"],
  ["plain_pair_blind_legacy", 0.0055, "rr-a873b3bdbaca07b8"],
  ["gu_marginal_conversion", 0.0055, "rr-966dd860b6d86a1b"],
  ["perrin_accurate_current_dem", 0.0055, "rr-48bf5dd280ce5801"],
  ["resolution", 0.0055, "rr-f8363fcc4d101bd4"],
  ["bare", 0.006, "rr-91912ea54ca1a54f"],
  ["plain_pair_blind_legacy", 0.006, "rr-d7152eb12581f27c"],
  ["gu_marginal_conversion", 0.006, "rr-afe80e2c96df58cc"],
  ["perrin_accurate_current_dem", 0.006, "rr-b4ed250312bcd55e"],
  ["resolution", 0.006, "rr-339d1fe6f5bc0f3a"],
  ["bare", 0.0065, "rr-3297b19133852fe4"],
  ["plain_pair_blind_legacy", 0.0065, "rr-1627181cb6dc1fb1"],
  ["gu_marginal_conversion", 0.0065, "rr-d55a55b61db96ad7"],
  ["perrin_accurate_current_dem", 0.0065, "rr-5003ce1ab967178e"],
  ["resolution", 0.0065, "rr-c542e51fa633fb13"],
];

const relayRunningPoints: ExperimentPoint[] = relayRunningRows.map((row) => {
  const key = `bb144|${row.method}|${row.pTotal.toFixed(4)}`;
  return {
    ...pointIds("relay100", key),
    canonicalKey: key,
    displayName: `BB144 / ${row.method} / p=${row.pTotal.toFixed(4)}`,
    status: "running",
    dimensions: { method: row.method, p_total: row.pTotal, server: row.server },
    metrics: {
      progress: reading(row.progress),
      shots: reading(row.shots),
      logical_errors: reading(row.logicalErrors),
    },
    sourceRecordId: row.runId,
    observationCount: row.shots,
    candidateCount: 1,
    settingDigest: digest(`relay100-setting:${key}`),
    pointRevisionDigest: digest(`relay100-snapshot:${key}:202`),
    artifacts: [{ label: "Controller snapshot", uri: "controller-snapshot:decoder_atomloss:202" }],
    runs: [{ runId: row.runId, role: "primary", status: "running" }],
  };
});

const relayQueuedPoints: ExperimentPoint[] = relayQueuedRows.map(([method, pTotal, queuedRunId]) => {
  const key = `bb144|${method}|${pTotal.toFixed(4)}`;
  return {
    ...pointIds("relay100", key),
    canonicalKey: key,
    displayName: `BB144 / ${method} / p=${pTotal.toFixed(4)}`,
    status: "queued",
    dimensions: { method, p_total: pTotal, server: null },
    metrics: {},
    sourceRecordId: queuedRunId,
    candidateCount: 1,
    settingDigest: digest(`relay100-setting:${key}`),
    pointRevisionDigest: digest(`relay100-snapshot:${key}:202`),
    artifacts: [{ label: "Controller snapshot", uri: "controller-snapshot:decoder_atomloss:202" }],
    runs: [{ runId: queuedRunId, role: "primary", status: "queued" }],
  };
});

interface HardwareEvidenceRow {
  key: string;
  displayName: string;
  checkpoint: string;
  fixture: string;
  status: ExperimentPointStatus;
  sourceRecordId: string;
  metrics?: Record<string, MetricReading>;
  observationCount?: number;
  reviewReason?: string;
  run?: ExperimentRunRef;
  artifact: ExperimentArtifactRef;
}

const hardwareEvidenceRows: HardwareEvidenceRow[] = [
  {
    key: "packed-reference-bb-hgp",
    displayName: "Packed exact reference / BB144 + HGP",
    checkpoint: "Packed reference",
    fixture: "BB144 + HGP",
    status: "complete",
    sourceRecordId: "packed-reference-20260725",
    metrics: { replay_cases: reading(4) },
    observationCount: 4,
    run: { runId: "rr-526e62d75b4460f1", role: "primary", status: "succeeded" },
    artifact: { label: "Packed reference evidence", uri: ".trellis/tasks/07-25-sharingan-resolution-hardware-abi/evidence/bb144-packed-reference-20260725.md" },
  },
  {
    key: "host-baseline-bb144-shot70",
    displayName: "16-bank host baseline / BB144 shot 70",
    checkpoint: "Host baseline",
    fixture: "BB144 shot 70",
    status: "complete",
    sourceRecordId: "host-baseline-bb144-shot70",
    metrics: { scheduled_cycles: reading(2767), replay_cases: reading(1) },
    observationCount: 1,
    artifact: { label: "HLS baseline evidence", uri: ".trellis/tasks/07-25-sharingan-resolution-hardware-abi/evidence/hgp-selected-hls-baseline-20260725.md" },
  },
  {
    key: "host-baseline-hgp-selected",
    displayName: "16-bank host baseline / HGP shots 17, 18",
    checkpoint: "Host baseline",
    fixture: "HGP selected",
    status: "complete",
    sourceRecordId: "host-baseline-hgp-selected",
    metrics: { scheduled_cycles: reading(1550), replay_cases: reading(2) },
    observationCount: 2,
    artifact: { label: "HGP selected evidence", uri: ".trellis/tasks/07-25-sharingan-resolution-hardware-abi/evidence/hgp-selected-hls-baseline-20260725.md" },
  },
  {
    key: "scan-microkernel-bb144-shot70",
    displayName: "Scan microkernel / BB144 shot 70",
    checkpoint: "Scan microkernel",
    fixture: "BB144 shot 70",
    status: "complete",
    sourceRecordId: "scan-microkernel-bb144-shot70",
    metrics: { raw_cycles: reading(1880), clock_ns: reading(3.58), lut: reading(198467), replay_cases: reading(1) },
    observationCount: 1,
    artifact: { label: "Implementation checkpoint", uri: ".trellis/tasks/07-25-sharingan-resolution-hardware-abi/implement.md" },
  },
  {
    key: "full-boundary-bb144-shot70",
    displayName: "Full boundary canary / BB144 shot 70",
    checkpoint: "Full boundary",
    fixture: "BB144 shot 70",
    status: "complete",
    sourceRecordId: "rr-f7556c7dd0a75caf",
    metrics: { raw_cycles: reading(4812), replay_cases: reading(1) },
    observationCount: 1,
    run: { runId: "rr-f7556c7dd0a75caf", role: "primary", status: "succeeded" },
    artifact: { label: "Implementation checkpoint", uri: ".trellis/tasks/07-25-sharingan-resolution-hardware-abi/implement.md" },
  },
  {
    key: "candidate-materializer-bb144-shot70",
    displayName: "Candidate materializer HLS / BB144 shot 70",
    checkpoint: "Materializer HLS",
    fixture: "BB144 shot 70",
    status: "running",
    sourceRecordId: "rr-88a2509aa800f2d7",
    run: { runId: "rr-88a2509aa800f2d7", role: "primary", status: "running" },
    artifact: { label: "Controller snapshot", uri: "controller-snapshot:decoder_atomloss:202" },
  },
  {
    key: "canonical-materializer-bb144-shot70",
    displayName: "Canonical materializer HLS / BB144 shot 70",
    checkpoint: "Materializer HLS",
    fixture: "BB144 shot 70 canonical",
    status: "running",
    sourceRecordId: "rr-964358966cbdaf93",
    run: { runId: "rr-964358966cbdaf93", role: "primary", status: "running" },
    artifact: { label: "Controller snapshot", uri: "controller-snapshot:decoder_atomloss:202" },
  },
  {
    key: "i0-census-bb144",
    displayName: "I0 capacity census / BB144 midpoint-500",
    checkpoint: "Capacity census",
    fixture: "BB144 midpoint-500",
    status: "running",
    sourceRecordId: "rr-9d1d08fa493b739d",
    metrics: { progress: reading(0.138), sampled_shots: reading(69), resolution_invocations: reading(3) },
    observationCount: 69,
    run: { runId: "rr-9d1d08fa493b739d", role: "replacement", status: "running" },
    artifact: { label: "Controller snapshot", uri: "controller-snapshot:decoder_atomloss:202" },
  },
  {
    key: "i0-census-hgp-rb9-b2",
    displayName: "I0 capacity census / HGP RB9 B2 midpoint-500",
    checkpoint: "Capacity census",
    fixture: "HGP RB9 B2 midpoint-500",
    status: "running",
    sourceRecordId: "rr-a0280cafe2e3b7ca",
    metrics: { progress: reading(0.41), sampled_shots: reading(205), resolution_invocations: reading(14) },
    observationCount: 205,
    run: { runId: "rr-a0280cafe2e3b7ca", role: "primary", status: "running" },
    artifact: { label: "Controller snapshot", uri: "controller-snapshot:decoder_atomloss:202" },
  },
  {
    key: "packed-rom-u200-bb70",
    displayName: "Packed-ROM U200 HLS / BB70",
    checkpoint: "U200 HLS",
    fixture: "BB70 packed ROM",
    status: "running",
    sourceRecordId: "rr-dc5d592764edd011",
    reviewReason: "Controller 权威状态仍为 running；该快照同时记录 remote runtime 缺失，observation 为 unknown，不能推断为失败或完成。",
    run: { runId: "rr-dc5d592764edd011", role: "primary", status: "running" },
    artifact: { label: "Controller snapshot", uri: "controller-snapshot:decoder_atomloss:202" },
  },
];

const hardwareEvidencePoints: ExperimentPoint[] = hardwareEvidenceRows.map((row) => ({
  ...pointIds("hardware", row.key),
  canonicalKey: row.key,
  displayName: row.displayName,
  status: row.status,
  dimensions: { checkpoint: row.checkpoint, fixture: row.fixture },
  metrics: row.metrics ?? {},
  sourceRecordId: row.sourceRecordId,
  observationCount: row.observationCount,
  candidateCount: 1,
  reviewReason: row.reviewReason,
  settingDigest: digest(`hardware-setting:${row.key}`),
  pointRevisionDigest: digest(`hardware-snapshot:${row.key}:202`),
  artifacts: [row.artifact],
  runs: row.run ? [row.run] : [],
}));

const canonicalCounts = emptyStatusCounts();
for (const point of canonicalLerPoints) canonicalCounts[point.status] += 1;

const relayCounts = emptyStatusCounts();
relayCounts.running = 11;
relayCounts.queued = 204;

const hardwareCounts = emptyStatusCounts();
hardwareCounts.complete = 5;
hardwareCounts.running = 5;

export const experimentDemoStudies: ExperimentStudy[] = [
  {
    studyId: "decoder-canonical-ler",
    canonicalKey: "section-6-canonical-ler",
    displayName: "§6 Canonical LER sweep",
    description: "主实验 288/288 个 canonical 点已存在；可按 code 切换查看，每个 code 包含 4 个 method × 6 个 p_total。69 个 relay_ours_050 点的 legacy per-shot 证据仍需补齐。",
    mode: "project-snapshot",
    activeRevisionId: "snapshot-20260727-canonical",
    planDigest: "sha256:b30d973eb2bc31802961a697712eef49fcc2cfc906f9da84a5356238353f3aec",
    eventCursor: 202,
    refreshedAt: experimentDemoProject.snapshotAt,
    dimensions: { code_label: "Code", method: "Method", p_total: "p_total" },
    metrics: [
      { key: "ler", label: "Logical error rate", shortLabel: "LER", format: "scientific", scale: "log" },
      { key: "ler_per_round", label: "LER per round", shortLabel: "LER / round", unit: "1/round", format: "scientific", scale: "log" },
      { key: "convergence_rate", label: "Convergence rate", shortLabel: "Convergence", format: "percent", scale: "linear" },
      { key: "logical_errors", label: "Logical errors", shortLabel: "Errors", format: "integer", scale: "linear" },
      { key: "shots", label: "Shots", shortLabel: "Shots", format: "integer", scale: "linear" },
      { key: "wall_time", label: "Wall time", shortLabel: "Wall time", unit: "s", format: "duration", scale: "linear" },
    ],
    primaryMetric: "ler",
    presentation: {
      xDimension: "p_total",
      seriesDimension: "method",
      rowDimension: "method",
      columnDimension: "p_total",
      facetDimensions: ["code_label"],
    },
    impact: { unchanged: 0, new: 0, stale: 0, archived: 0 },
    points: canonicalLerPoints,
    pointCount: canonicalLerPoints.length,
    statusCounts: canonicalCounts,
  },
  {
    studyId: "decoder-relay100-active",
    canonicalKey: "relay100-main-sweep-20260727",
    displayName: "Relay-100 main sweep",
    description: "Controller 序列 202：全任务 11 running、2 registered、202 queued；registered 在摘要中计入待调度。下表展示 BB144 的 5 个运行中与 27 个排队点。",
    mode: "project-snapshot",
    activeRevisionId: "controller-sequence-202",
    planDigest: digest("relay100-main-sweep-20260727:controller-sequence-202"),
    eventCursor: 202,
    refreshedAt: experimentDemoProject.snapshotAt,
    dimensions: { method: "Method", p_total: "p_total", server: "Server" },
    metrics: [
      { key: "progress", label: "Controller progress", shortLabel: "Progress", format: "percent", scale: "linear" },
      { key: "shots", label: "Cumulative shots", shortLabel: "Shots", format: "integer", scale: "linear" },
      { key: "logical_errors", label: "Logical errors", shortLabel: "Errors", format: "integer", scale: "linear" },
    ],
    primaryMetric: "progress",
    presentation: {
      xDimension: "p_total",
      seriesDimension: "method",
      rowDimension: "method",
      columnDimension: "p_total",
    },
    impact: { unchanged: 0, new: 0, stale: 0, archived: 0 },
    points: [...relayRunningPoints, ...relayQueuedPoints],
    pointCount: 215,
    statusCounts: relayCounts,
  },
  {
    studyId: "decoder-resolution-hardware",
    canonicalKey: "07-25-sharingan-resolution-hardware-abi",
    displayName: "Resolution hardware verification",
    description: "已记录 5 组 supporting evidence，并映射 Controller 中 5 个权威 active run；它们不等同于完整 RTL fixture matrix 或 production acceptance。",
    mode: "project-snapshot",
    activeRevisionId: "snapshot-20260727-hardware",
    planDigest: digest("sharingan-resolution-hardware-abi:20260727"),
    eventCursor: 202,
    refreshedAt: experimentDemoProject.snapshotAt,
    dimensions: { checkpoint: "Checkpoint", fixture: "Fixture" },
    metrics: [
      { key: "raw_cycles", label: "Raw RTL cycles", shortLabel: "Raw cycles", unit: "cycles", format: "integer", scale: "linear" },
      { key: "scheduled_cycles", label: "Scheduled issue cycles", shortLabel: "Scheduled", unit: "cycles", format: "integer", scale: "linear" },
      { key: "clock_ns", label: "Estimated clock", shortLabel: "Clock", unit: "ns", format: "decimal", scale: "linear" },
      { key: "lut", label: "Estimated LUT", shortLabel: "LUT", format: "integer", scale: "linear" },
      { key: "progress", label: "Controller progress", shortLabel: "Progress", format: "percent", scale: "linear" },
      { key: "sampled_shots", label: "Sampled shots", shortLabel: "Shots", format: "integer", scale: "linear" },
      { key: "resolution_invocations", label: "Resolution invocations", shortLabel: "Invoked", format: "integer", scale: "linear" },
      { key: "replay_cases", label: "Validated replay cases", shortLabel: "Replays", format: "integer", scale: "linear" },
    ],
    primaryMetric: "raw_cycles",
    presentation: {
      xDimension: "checkpoint",
      seriesDimension: "fixture",
      rowDimension: "checkpoint",
      columnDimension: "fixture",
    },
    impact: { unchanged: 0, new: 0, stale: 0, archived: 0 },
    points: hardwareEvidencePoints,
    pointCount: 10,
    statusCounts: hardwareCounts,
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
  const counts = emptyStatusCounts();
  for (const point of study.points) counts[point.status] += 1;
  return counts;
}
