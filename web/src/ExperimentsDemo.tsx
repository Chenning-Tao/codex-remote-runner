import {
  Alert,
  Button,
  Drawer,
  DrawerActions,
  DrawerCloseButton,
  DrawerContent,
  DrawerContentBody,
  DrawerHead,
  DrawerPanelBody,
  DrawerPanelContent,
  Label,
  SearchInput,
  Tab,
  Tabs,
  TabTitleText,
  TextArea,
  ToggleGroup,
  ToggleGroupItem,
  Tooltip,
} from "@patternfly/react-core";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  CircleDashed,
  CircleX,
  Clock3,
  Database,
  GitBranch,
  History,
  ListChecks,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  experimentDemoProject,
  experimentDemoStudies,
  formatMetricValue,
  pointStatusCounts,
  type ExperimentMetric,
  type ExperimentPoint,
  type ExperimentPointStatus,
  type ExperimentScalar,
  type ExperimentStudy,
} from "./experimentDemoData";
import { ProductNav } from "./ProductNav";
import { useExperiments } from "./useExperiments";

type ExperimentView = "results" | "curves" | "matrix";
type PointFilter = "all" | "complete" | "attention";
const LAST_EXPERIMENT_STUDY_KEY = "remote-runner:last-experiment-study";

const statusMeta: Record<
  ExperimentPointStatus,
  { label: string; color: "green" | "blue" | "orange" | "red" | "grey"; icon: ReactNode }
> = {
  complete: { label: "已接受", color: "green", icon: <CheckCircle2 /> },
  running: { label: "运行中", color: "blue", icon: <Activity /> },
  queued: { label: "排队中", color: "blue", icon: <Clock3 /> },
  review: { label: "待接受", color: "orange", icon: <ListChecks /> },
  failed: { label: "失败", color: "red", icon: <CircleX /> },
  stale: { label: "已过期", color: "orange", icon: <History /> },
  planned: { label: "待运行", color: "grey", icon: <CircleDashed /> },
};

const seriesColors = ["#0071e3", "#248a3d", "#c93400", "#6e6e73"];
const dashPatterns = [undefined, "7 4", "2 4", "10 3 2 3"];
const runRoleLabels = {
  primary: "主运行",
  continuation: "续跑",
  replacement: "替代运行",
  aggregation: "聚合运行",
};
const runStatusLabels = {
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  unknown: "状态未知",
};
const resultDispositionLabels = {
  accepted: "已接受",
  rejected: "已拒绝",
  superseded: "已取代",
  review: "待接受",
  retained: "历史保留",
};

function ChartPointShape({
  seriesIndex,
  x,
  y,
  color,
  size = 5,
}: {
  seriesIndex: number;
  x: number;
  y: number;
  color: string;
  size?: number;
}) {
  const common = { className: "rr-exp-chart-point", fill: color };
  switch (seriesIndex % 4) {
    case 1:
      return <rect {...common} x={x - size} y={y - size} width={size * 2} height={size * 2} rx={1} />;
    case 2:
      return <polygon {...common} points={`${x},${y - size - 1} ${x + size + 1},${y + size} ${x - size - 1},${y + size}`} />;
    case 3:
      return <polygon {...common} points={`${x},${y - size - 1} ${x + size + 1},${y} ${x},${y + size + 1} ${x - size - 1},${y}`} />;
    default:
      return <circle {...common} cx={x} cy={y} r={size} />;
  }
}

function requestedStudyId(): string {
  const requested = new URLSearchParams(window.location.search).get("study");
  if (requested) return requested;
  try {
    return sessionStorage.getItem(LAST_EXPERIMENT_STUDY_KEY) ?? "";
  } catch {
    return "";
  }
}

function requestedCodeLabel(): string {
  return new URLSearchParams(window.location.search).get("code") ?? "";
}

function newAcceptanceId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  return `acceptance-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function initialView(): ExperimentView {
  const requested = new URLSearchParams(window.location.search).get("tab");
  return requested === "curves" || requested === "matrix" ? requested : "results";
}

function isSnapshotStudy(study: ExperimentStudy): boolean {
  return study.mode === "project-snapshot";
}

function statusLabel(study: ExperimentStudy, status: ExperimentPointStatus): string {
  if (!isSnapshotStudy(study)) return statusMeta[status].label;
  if (status === "complete") return "已记录";
  if (status === "review") return "证据待补齐";
  return statusMeta[status].label;
}

function availablePointCount(study: ExperimentStudy): number {
  const counts = pointStatusCounts(study);
  return isSnapshotStudy(study) ? counts.complete + counts.review : counts.complete;
}

function isChartablePoint(study: ExperimentStudy, point: ExperimentPoint): boolean {
  return point.status === "complete" || (isSnapshotStudy(study) && point.status === "review");
}

function StatusLabel({ study, status }: { study: ExperimentStudy; status: ExperimentPointStatus }) {
  const visual = statusMeta[status];
  return (
    <Label isCompact color={visual.color} variant="outline" icon={visual.icon}>
      {statusLabel(study, status)}
    </Label>
  );
}

function metricByKey(study: ExperimentStudy, key: string): ExperimentMetric {
  return study.metrics.find((metric) => metric.key === key) ?? study.metrics[0];
}

function formatInterval(point: ExperimentPoint, metric: ExperimentMetric): string {
  const interval = point.metrics[metric.key]?.interval;
  if (!interval) return "--";
  return `${formatMetricValue(interval.lower, metric)} - ${formatMetricValue(interval.upper, metric)}`;
}

function dimensionValue(value: ExperimentScalar | undefined): string {
  if (value === null || typeof value === "undefined") return "--";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value !== "number") return value;
  return value.toLocaleString("zh-CN", {
    maximumFractionDigits: Number.isInteger(value) ? 0 : 8,
  });
}

function numericMetricValue(point: ExperimentPoint, metricKey: string): number | undefined {
  const value = point.metrics[metricKey]?.value;
  return typeof value === "number" ? value : undefined;
}

function metricLabel(metric: ExperimentMetric): string {
  return metric.unit ? `${metric.shortLabel} (${metric.unit})` : metric.shortLabel;
}

function pointMatchesFilter(study: ExperimentStudy, point: ExperimentPoint, filter: PointFilter): boolean {
  if (filter === "complete") return isChartablePoint(study, point);
  if (filter === "attention") {
    return ["review", "failed", "stale", "planned"].includes(point.status);
  }
  return true;
}

function StudyRail({
  studies,
  selectedStudyId,
  onSelect,
}: {
  studies: ExperimentStudy[];
  selectedStudyId: string;
  onSelect: (studyId: string) => void;
}) {
  const snapshot = studies.every(isSnapshotStudy);
  return (
    <aside className="rr-exp-study-rail" aria-labelledby="rr-exp-study-list-title">
      <header className="rr-exp-study-rail-header">
        <div>
          <h2 id="rr-exp-study-list-title">实验组</h2>
          <p>{snapshot ? "项目快照" : "当前设计版本"}</p>
        </div>
        <span className="rr-count rr-mono">{studies.length}</span>
      </header>
      <div className="rr-exp-study-list">
        {studies.map((study) => {
          const counts = pointStatusCounts(study);
          const pointCount = study.pointCount ?? study.points.length;
          const available = availablePointCount(study);
          const percent = pointCount ? Math.round((available / pointCount) * 100) : 0;
          const needsAttention = counts.failed + counts.stale + counts.review + counts.planned;
          return (
            <button
              key={study.studyId}
              type="button"
              className={`rr-exp-study-row ${study.studyId === selectedStudyId ? "rr-exp-study-row-selected" : ""}`}
              aria-current={study.studyId === selectedStudyId ? "true" : undefined}
              onClick={() => onSelect(study.studyId)}
            >
              <span className="rr-exp-study-row-copy">
                <strong>{study.displayName}</strong>
                <small className="rr-mono">{study.canonicalKey}</small>
              </span>
              <span className="rr-exp-study-row-progress">
                <span className="rr-exp-progress-track" aria-hidden="true">
                  <span style={{ width: `${percent}%` }} />
                </span>
                <span>
                  {isSnapshotStudy(study) ? "有记录" : "已接受"} {available}/{pointCount}
                  {needsAttention > 0 && <em>{needsAttention} 需关注</em>}
                </span>
              </span>
              <span className="rr-exp-study-row-revision rr-mono">{study.activeRevisionId}</span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}

function ResultsTable({
  study,
  points,
  metric,
  onSelect,
}: {
  study: ExperimentStudy;
  points: ExperimentPoint[];
  metric: ExperimentMetric;
  onSelect: (point: ExperimentPoint) => void;
}) {
  const dimensionKeys = Object.keys(study.dimensions);
  const hasIntervals = study.points.some((point) => point.metrics[metric.key]?.interval);
  const hasRuns = study.points.some((point) => point.runs.length > 0);
  return (
    <div className="rr-table-scroll">
      <table className="rr-exp-results-table">
        <thead>
          <tr>
            <th>状态</th>
            <th>实验点</th>
            {dimensionKeys.map((key) => <th key={key}>{study.dimensions[key]}</th>)}
            <th>{metricLabel(metric)}</th>
            {hasIntervals && <th>95% 区间</th>}
            <th>证据量</th>
            {hasRuns && <th>运行数</th>}
          </tr>
        </thead>
        <tbody>
          {points.map((point) => {
            const reading = point.metrics[metric.key];
            return (
              <tr key={point.pointRevisionId}>
                <td><StatusLabel study={study} status={point.status} /></td>
                <td>
                  <button className="rr-exp-point-link" type="button" onClick={() => onSelect(point)}>
                    <strong>{point.displayName}</strong>
                    <small className="rr-mono">{point.pointRevisionId}</small>
                  </button>
                </td>
                {dimensionKeys.map((key) => (
                  <td key={key} className="rr-exp-dimension-value">
                    {dimensionValue(point.dimensions[key])}
                  </td>
                ))}
                <td className="rr-exp-metric-value rr-mono">
                  {formatMetricValue(reading?.value, metric)}
                </td>
                {hasIntervals && <td className="rr-exp-interval rr-mono">{formatInterval(point, metric)}</td>}
                <td className="rr-mono">
                  {typeof point.observationCount === "number"
                    ? point.observationCount.toLocaleString("zh-CN")
                    : "--"}
                </td>
                {hasRuns && <td className="rr-mono">{point.runs.length || "--"}</td>}
              </tr>
            );
          })}
        </tbody>
      </table>
      {points.length === 0 && <div className="rr-empty">当前筛选没有匹配的实验点。</div>}
    </div>
  );
}

function CurveChart({
  study,
  points,
  metric,
}: {
  study: ExperimentStudy;
  points: ExperimentPoint[];
  metric: ExperimentMetric;
}) {
  const chartable = points.filter(
    (point) => isChartablePoint(study, point) && numericMetricValue(point, metric.key) !== undefined,
  );
  const xKey = study.presentation.xDimension;
  const seriesKey = study.presentation.seriesDimension;
  const xValues = Array.from(new Set(chartable.map((point) => point.dimensions[xKey]))).sort((a, b) => {
    if (typeof a === "number" && typeof b === "number") return a - b;
    return String(a).localeCompare(String(b));
  });
  const series = Array.from(new Set(chartable.map((point) => String(point.dimensions[seriesKey]))));
  const values = chartable.map((point) => numericMetricValue(point, metric.key)!);

  if (values.length === 0) {
    return <div className="rr-empty">当前筛选没有带该指标的{isSnapshotStudy(study) ? "可展示记录" : "已接受结果"}。</div>;
  }

  const width = 860;
  const height = 350;
  const margin = { top: 26, right: 28, bottom: 58, left: 90 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const positiveValues = values.filter((value) => value > 0);
  const useLog = metric.scale === "log" && positiveValues.length === values.length;
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const lower = useLog ? Math.log10(minValue) : Math.min(0, minValue);
  const upper = useLog ? Math.log10(maxValue) : maxValue;
  const valueSpan = upper - lower || 1;
  const xAt = (value: ExperimentScalar | undefined) => {
    const index = xValues.findIndex((candidate) => candidate === value);
    return margin.left + (xValues.length === 1 ? plotWidth / 2 : (index / (xValues.length - 1)) * plotWidth);
  };
  const yAt = (value: number) => {
    const normalized = ((useLog ? Math.log10(value) : value) - lower) / valueSpan;
    return margin.top + plotHeight - normalized * plotHeight;
  };
  const ticks = Array.from({ length: 5 }, (_, index) => {
    const normalized = index / 4;
    const raw = lower + (upper - lower) * normalized;
    return useLog ? 10 ** raw : raw;
  });

  return (
    <div className="rr-exp-curve-wrap">
      <div className="rr-exp-curve-meta">
        <span><CheckCircle2 aria-hidden="true" />当前筛选内 {chartable.length} 个{isSnapshotStudy(study) ? "可展示记录" : "已接受实验点"}</span>
        <span><History aria-hidden="true" />{isSnapshotStudy(study) ? "证据待补齐的记录仍按 canonical 值展示" : "已排除过期结果"}</span>
      </div>
      <div className="rr-exp-chart-scroll">
        <svg
          className="rr-exp-chart"
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-labelledby="rr-exp-chart-title rr-exp-chart-description"
        >
          <title id="rr-exp-chart-title">{metricLabel(metric)}，按 {study.dimensions[xKey]} 展示</title>
          <desc id="rr-exp-chart-description">
            当前筛选中的{isSnapshotStudy(study) ? "项目快照记录" : "已接受结果"}按 {study.dimensions[seriesKey]} 分组。
          </desc>
          {ticks.map((tick) => {
            const y = yAt(tick);
            return (
              <g key={tick}>
                <line className="rr-exp-chart-grid" x1={margin.left} x2={width - margin.right} y1={y} y2={y} />
                <text className="rr-exp-chart-tick" x={margin.left - 12} y={y + 4} textAnchor="end">
                  {formatMetricValue(tick, metric)}
                </text>
              </g>
            );
          })}
          <line className="rr-exp-chart-axis" x1={margin.left} x2={margin.left} y1={margin.top} y2={height - margin.bottom} />
          <line className="rr-exp-chart-axis" x1={margin.left} x2={width - margin.right} y1={height - margin.bottom} y2={height - margin.bottom} />
          {xValues.map((value) => (
            <g key={String(value)}>
              <line className="rr-exp-chart-axis-mark" x1={xAt(value)} x2={xAt(value)} y1={height - margin.bottom} y2={height - margin.bottom + 6} />
              <text className="rr-exp-chart-tick" x={xAt(value)} y={height - margin.bottom + 24} textAnchor="middle">
                {dimensionValue(value)}
              </text>
            </g>
          ))}
          {series.map((seriesName, seriesIndex) => {
            const points = chartable
              .filter((point) => String(point.dimensions[seriesKey]) === seriesName)
              .sort((a, b) => xValues.indexOf(a.dimensions[xKey]) - xValues.indexOf(b.dimensions[xKey]));
            const path = points.map((point, index) => {
              const x = xAt(point.dimensions[xKey]);
              const y = yAt(numericMetricValue(point, metric.key)!);
              return `${index === 0 ? "M" : "L"}${x},${y}`;
            }).join(" ");
            const color = seriesColors[seriesIndex % seriesColors.length];
            return (
              <g key={seriesName}>
                {points.length > 1 && (
                  <path
                    className="rr-exp-chart-series"
                    d={path}
                    stroke={color}
                    strokeDasharray={dashPatterns[seriesIndex % dashPatterns.length]}
                  />
                )}
                {points.map((point) => {
                  const reading = point.metrics[metric.key];
                  const value = numericMetricValue(point, metric.key)!;
                  const x = xAt(point.dimensions[xKey]);
                  const y = yAt(value);
                  return (
                    <g key={point.pointRevisionId}>
                      <title>{point.displayName}: {formatMetricValue(value, metric)}</title>
                      {reading.interval && (
                        <line
                          className="rr-exp-chart-interval"
                          x1={x}
                          x2={x}
                          y1={yAt(reading.interval.lower)}
                          y2={yAt(reading.interval.upper)}
                          stroke={color}
                        />
                      )}
                      <ChartPointShape seriesIndex={seriesIndex} x={x} y={y} color={color} />
                    </g>
                  );
                })}
              </g>
            );
          })}
          <text className="rr-exp-chart-label" x={margin.left + plotWidth / 2} y={height - 8} textAnchor="middle">
            {study.dimensions[xKey]}
          </text>
          <text
            className="rr-exp-chart-label"
            x={18}
            y={margin.top + plotHeight / 2}
            textAnchor="middle"
            transform={`rotate(-90 18 ${margin.top + plotHeight / 2})`}
          >
            {metricLabel(metric)}
          </text>
        </svg>
      </div>
      <div className="rr-exp-chart-legend" aria-label="曲线图例">
        {series.map((seriesName, index) => (
          <span key={seriesName}>
            <svg className="rr-exp-chart-legend-mark" viewBox="0 0 28 14" aria-hidden="true">
              <line
                x1="1"
                x2="27"
                y1="7"
                y2="7"
                stroke={seriesColors[index % seriesColors.length]}
                strokeWidth="2"
                strokeDasharray={dashPatterns[index % dashPatterns.length]}
              />
              <ChartPointShape seriesIndex={index} x={14} y={7} color={seriesColors[index % seriesColors.length]} size={3.5} />
            </svg>
            {seriesName}
          </span>
        ))}
      </div>
    </div>
  );
}

function PointMatrix({
  study,
  points,
  metric,
  onSelect,
}: {
  study: ExperimentStudy;
  points: ExperimentPoint[];
  metric: ExperimentMetric;
  onSelect: (point: ExperimentPoint) => void;
}) {
  const rowKey = study.presentation.rowDimension;
  const columnKey = study.presentation.columnDimension;
  const rows = Array.from(new Set(points.map((point) => point.dimensions[rowKey])));
  const columns = Array.from(new Set(points.map((point) => point.dimensions[columnKey]))).sort((a, b) => {
    if (typeof a === "number" && typeof b === "number") return a - b;
    return String(a).localeCompare(String(b));
  });

  return (
    <div className="rr-table-scroll rr-exp-matrix-scroll">
      <table className="rr-exp-matrix">
        <thead>
          <tr>
            <th>{study.dimensions[rowKey]} / {study.dimensions[columnKey]}</th>
            {columns.map((column) => <th key={String(column)}>{dimensionValue(column)}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row)}>
              <th>{dimensionValue(row)}</th>
              {columns.map((column) => {
                const point = points.find(
                  (candidate) => candidate.dimensions[rowKey] === row && candidate.dimensions[columnKey] === column,
                );
                if (!point) return <td key={String(column)} className="rr-exp-matrix-empty">--</td>;
                const reading = point.metrics[metric.key];
                return (
                  <td key={String(column)}>
                    <button
                      type="button"
                      className={`rr-exp-matrix-cell rr-exp-matrix-${point.status}`}
                      aria-label={`${point.displayName}: ${statusLabel(study, point.status)}, ${formatMetricValue(reading?.value, metric)}`}
                      onClick={() => onSelect(point)}
                    >
                      <span>{statusMeta[point.status].icon}{statusLabel(study, point.status)}</span>
                      <strong className="rr-mono">{formatMetricValue(reading?.value, metric)}</strong>
                    </button>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {points.length === 0 && <div className="rr-empty">当前筛选没有匹配的实验点。</div>}
    </div>
  );
}

function PointDetail({
  study,
  point,
  onClose,
  onDecision,
  detailError,
}: {
  study: ExperimentStudy;
  point: ExperimentPoint;
  onClose: () => void;
  onDecision?: (
    acceptanceId: string,
    resultId: string,
    action: "accept" | "reject",
    reason: string,
  ) => Promise<void>;
  detailError?: string | null;
}) {
  const closeButtonRef = useRef<HTMLDivElement>(null);
  const snapshot = isSnapshotStudy(study);
  const currentRecordId = snapshot ? point.sourceRecordId : point.acceptedResultId;
  const hasCurrentMetrics = Object.keys(point.metrics).length > 0;
  const [pendingDecision, setPendingDecision] = useState<{
    acceptanceId: string;
    resultId: string;
    action: "accept" | "reject";
  } | null>(null);
  const [decisionReason, setDecisionReason] = useState("");
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [savingDecision, setSavingDecision] = useState(false);

  useEffect(() => {
    closeButtonRef.current?.querySelector("button")?.focus();
    setPendingDecision(null);
    setDecisionReason("");
    setDecisionError(null);
    setSavingDecision(false);
  }, [point.pointRevisionId]);

  async function submitDecision() {
    if (!pendingDecision || !onDecision || !decisionReason.trim()) return;
    setSavingDecision(true);
    setDecisionError(null);
    try {
      await onDecision(
        pendingDecision.acceptanceId,
        pendingDecision.resultId,
        pendingDecision.action,
        decisionReason.trim(),
      );
    } catch (error: unknown) {
      setDecisionError(error instanceof Error ? error.message : "实验结果决策失败");
      setSavingDecision(false);
    }
  }

  return (
    <DrawerPanelContent className="rr-detail-panel rr-exp-detail-panel" focusTrap={{ enabled: true, "aria-labelledby": "rr-exp-detail-title" }}>
      <DrawerHead>
        <div>
          <p className="rr-eyebrow">实验点详情</p>
          <h2 id="rr-exp-detail-title">{point.displayName}</h2>
        </div>
        <DrawerActions>
          <DrawerCloseButton ref={closeButtonRef} aria-label="关闭 point 详情" onClick={onClose} />
        </DrawerActions>
      </DrawerHead>
      <DrawerPanelBody>
        <div className="rr-detail-status"><StatusLabel study={study} status={point.status} /></div>
        {detailError && (
          <Alert isInline variant="danger" title="候选结果读取失败" className="rr-alert">
            {detailError}
          </Alert>
        )}
        {(point.reviewReason || point.staleReason || point.failureReason) && (
          <div className={`rr-exp-detail-notice ${point.failureReason ? "rr-exp-detail-notice-danger" : ""}`} role="status">
            <AlertCircle aria-hidden="true" />
            <div>
              <strong>{point.failureReason ? "当前结果不可用" : point.reviewReason ? "来源证据待补齐" : "当前版本已变化"}</strong>
              <span>{point.failureReason ?? point.reviewReason ?? point.staleReason}</span>
            </div>
          </div>
        )}
        <section className="rr-exp-detail-section" aria-labelledby="rr-exp-detail-identity">
          <h3 id="rr-exp-detail-identity">标识</h3>
          <dl className="rr-exp-detail-list">
            <div><dt>实验点 ID</dt><dd className="rr-mono">{point.pointId}</dd></div>
            <div><dt>实验点版本</dt><dd className="rr-mono">{point.pointRevisionId}</dd></div>
            <div><dt>设计版本</dt><dd className="rr-mono">{study.activeRevisionId}</dd></div>
            <div><dt>设置摘要</dt><dd className="rr-mono">{point.settingDigest}</dd></div>
            <div><dt>要求摘要</dt><dd className="rr-mono">{point.pointRevisionDigest}</dd></div>
          </dl>
        </section>
        <section className="rr-exp-detail-section" aria-labelledby="rr-exp-detail-dimensions">
          <h3 id="rr-exp-detail-dimensions">维度</h3>
          <dl className="rr-exp-detail-list">
            {Object.entries(point.dimensions).map(([key, value]) => (
              <div key={key}><dt>{study.dimensions[key] ?? key}</dt><dd>{dimensionValue(value)}</dd></div>
            ))}
          </dl>
        </section>
        <section className="rr-exp-detail-section" aria-labelledby="rr-exp-detail-result">
          <h3 id="rr-exp-detail-result">{snapshot ? "当前快照记录" : "当前已接受结果"}</h3>
          {currentRecordId || hasCurrentMetrics ? (
            <>
              {currentRecordId && <p className="rr-exp-result-id rr-mono">{currentRecordId}</p>}
              <dl className="rr-exp-detail-list">
                {study.metrics.map((metric) => (
                  <div key={metric.key}>
                    <dt>{metric.label}</dt>
                    <dd className="rr-mono">
                      {formatMetricValue(point.metrics[metric.key]?.value, metric)}
                      {point.metrics[metric.key] && metric.unit && metric.format !== "duration" ? ` ${metric.unit}` : ""}
                    </dd>
                  </div>
                ))}
                <div><dt>观测数</dt><dd className="rr-mono">{point.observationCount?.toLocaleString("zh-CN") ?? "--"}</dd></div>
                <div><dt>{snapshot ? "来源记录数" : "候选结果数"}</dt><dd className="rr-mono">{point.candidateCount}</dd></div>
              </dl>
            </>
          ) : <p className="rr-exp-detail-empty">{snapshot ? "这个快照点还没有结果记录。" : "这个实验点版本没有已接受结果。"}</p>}
        </section>
        {!snapshot && !detailError && point.candidateCount > 0 && point.resultHistory === undefined && (
          <section className="rr-exp-detail-section" aria-label="正在读取候选结果">
            <div className="rr-exp-detail-loading" role="status">
              <LoaderCircle className="rr-spin" aria-hidden="true" />
              正在读取候选指标与证据...
            </div>
          </section>
        )}
        {point.resultHistory && point.resultHistory.length > 0 && (
          <section className="rr-exp-detail-section" aria-labelledby="rr-exp-detail-candidates">
            <h3 id="rr-exp-detail-candidates">候选结果历史</h3>
            <div className="rr-exp-candidate-list">
              {point.resultHistory.map((candidate) => (
                <article className="rr-exp-candidate" key={candidate.resultId}>
                  <header>
                    <span className={`rr-exp-disposition rr-exp-disposition-${candidate.disposition}`}>
                      {resultDispositionLabels[candidate.disposition]}
                    </span>
                    <code className="rr-mono">{candidate.resultId}</code>
                  </header>
                  <dl className="rr-exp-candidate-evidence">
                    {study.metrics.flatMap((metric) => {
                      const reading = candidate.metrics?.[metric.key];
                      if (!reading) return [];
                      return [(
                        <div key={metric.key}>
                          <dt>{metric.label}</dt>
                          <dd className="rr-mono">
                            {formatMetricValue(reading.value, metric)}
                            {metric.unit && metric.format !== "duration" ? ` ${metric.unit}` : ""}
                          </dd>
                        </div>
                      )];
                    })}
                    <div>
                      <dt>观测数</dt>
                      <dd className="rr-mono">{candidate.observationCount?.toLocaleString("zh-CN") ?? "--"}</dd>
                    </div>
                    <div>
                      <dt>来源运行</dt>
                      <dd className="rr-mono">{candidate.sourceRunIds.length || "--"}</dd>
                    </div>
                  </dl>
                  {candidate.ineligibilityReasons && candidate.ineligibilityReasons.length > 0 && (
                    <p className="rr-exp-candidate-reasons">
                      {candidate.ineligibilityReasons.join("；")}
                    </p>
                  )}
                  {candidate.disposition === "review" && onDecision && (
                    pendingDecision?.resultId === candidate.resultId ? (
                      <div className="rr-exp-decision-editor">
                        <strong>
                          {pendingDecision.action === "accept" ? "确认接受这个候选结果" : "确认拒绝这个候选结果"}
                        </strong>
                        <TextArea
                          aria-label="决策理由"
                          value={decisionReason}
                          placeholder="记录判断依据"
                          resizeOrientation="vertical"
                          isDisabled={savingDecision}
                          onChange={(_event, value) => setDecisionReason(value)}
                        />
                        {decisionError && <div className="rr-stop-error" role="alert">{decisionError}</div>}
                        <div className="rr-exp-decision-confirm-actions">
                          <Button
                            variant={pendingDecision.action === "accept" ? "primary" : "danger"}
                            icon={savingDecision ? <LoaderCircle className="rr-spin" /> : pendingDecision.action === "accept" ? <CheckCircle2 /> : <CircleX />}
                            isDisabled={savingDecision || !decisionReason.trim()}
                            onClick={() => void submitDecision()}
                          >
                            {savingDecision
                              ? "正在记录..."
                              : pendingDecision.action === "accept"
                                ? "确认接受"
                                : "确认拒绝"}
                          </Button>
                          <Button
                            variant="link"
                            isDisabled={savingDecision}
                            onClick={() => {
                              setPendingDecision(null);
                              setDecisionReason("");
                              setDecisionError(null);
                            }}
                          >
                            取消
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <div className="rr-exp-candidate-actions">
                        <Button
                          variant="primary"
                          size="sm"
                          icon={<CheckCircle2 />}
                          onClick={() => {
                            setPendingDecision({ acceptanceId: newAcceptanceId(), resultId: candidate.resultId, action: "accept" });
                            setDecisionReason("");
                            setDecisionError(null);
                          }}
                        >
                          接受
                        </Button>
                        <Button
                          variant="danger"
                          size="sm"
                          icon={<CircleX />}
                          onClick={() => {
                            setPendingDecision({ acceptanceId: newAcceptanceId(), resultId: candidate.resultId, action: "reject" });
                            setDecisionReason("");
                            setDecisionError(null);
                          }}
                        >
                          拒绝
                        </Button>
                      </div>
                    )
                  )}
                </article>
              ))}
            </div>
          </section>
        )}
        {point.historicalMetrics && (
          <section className="rr-exp-detail-section rr-exp-historical" aria-labelledby="rr-exp-detail-history">
            <h3 id="rr-exp-detail-history">历史版本结果</h3>
            <p>仅保留用于审计，不进入当前结果或曲线。</p>
            <dl className="rr-exp-detail-list">
              {study.metrics.map((metric) => (
                <div key={metric.key}>
                  <dt>{metric.label}</dt>
                  <dd className="rr-mono">
                    {formatMetricValue(point.historicalMetrics?.[metric.key]?.value, metric)}
                    {point.historicalMetrics?.[metric.key] && metric.unit && metric.format !== "duration" ? ` ${metric.unit}` : ""}
                  </dd>
                </div>
              ))}
            </dl>
          </section>
        )}
        {point.changedComponents && point.changedComponents.length > 0 && (
          <section className="rr-exp-detail-section" aria-labelledby="rr-exp-detail-components">
            <h3 id="rr-exp-detail-components">变更组件</h3>
            <ul className="rr-exp-component-list">
              {point.changedComponents.map((component) => <li key={component} className="rr-mono">{component}</li>)}
            </ul>
          </section>
        )}
        {point.artifacts && point.artifacts.length > 0 && (
          <section className="rr-exp-detail-section" aria-labelledby="rr-exp-detail-artifacts">
            <h3 id="rr-exp-detail-artifacts">规范化产物引用</h3>
            <ul className="rr-exp-artifact-list">
              {point.artifacts.map((artifact) => (
                <li key={artifact.uri}>
                  <span>{artifact.label}</span>
                  <code className="rr-mono">{artifact.uri}</code>
                </li>
              ))}
            </ul>
          </section>
        )}
        <section className="rr-exp-detail-section" aria-labelledby="rr-exp-detail-runs">
          <h3 id="rr-exp-detail-runs">绑定运行</h3>
          {point.runs.length ? (
            <ul className="rr-exp-run-list">
              {point.runs.map((run) => (
                <li key={run.runId}>
                  <span className={`rr-state-dot rr-state-${run.status}`} aria-hidden="true" />
                  <span className="rr-mono">{run.runId}</span>
                  <small>{runRoleLabels[run.role]} · {runStatusLabels[run.status]}</small>
                </li>
              ))}
            </ul>
          ) : <p className="rr-exp-detail-empty">还没有绑定 run。</p>}
        </section>
      </DrawerPanelBody>
    </DrawerPanelContent>
  );
}

export function ExperimentsDashboard({ live = false }: { live?: boolean }) {
  const [studyId, setStudyId] = useState(requestedStudyId);
  const [view, setView] = useState<ExperimentView>(initialView);
  const [filter, setFilter] = useState<PointFilter>("all");
  const [query, setQuery] = useState("");
  const [metricKey, setMetricKey] = useState("");
  const [codeLabel, setCodeLabel] = useState(requestedCodeLabel);
  const [selectedPoint, setSelectedPoint] = useState<ExperimentPoint | null>(null);
  const [pointDetailError, setPointDetailError] = useState<string | null>(null);
  const [decisionNotice, setDecisionNotice] = useState<string | null>(null);
  const [refreshedAt, setRefreshedAt] = useState(Date.now());
  const registry = useExperiments(live, studyId || null);
  const studies = live ? registry.studies : experimentDemoStudies;
  const baseStudy = studies.find((candidate) => candidate.studyId === studyId) ?? studies[0];
  const study = baseStudy && live
    ? {
        ...baseStudy,
        points: registry.pointsStudyId === baseStudy.studyId ? registry.points : [],
      }
    : baseStudy;

  useEffect(() => {
    if (!studies.length) return;
    setStudyId((current) => (
      studies.some((candidate) => candidate.studyId === current)
        ? current
        : studies[0].studyId
    ));
  }, [studies]);

  useEffect(() => {
    if (live && !registry.loadingStudies && !registry.loadingPoints) {
      setRefreshedAt(Date.now());
    }
  }, [live, registry.eventCursor, registry.loadingPoints, registry.loadingStudies]);

  const codeDimensionKey = study
    ? study.presentation.facetDimensions?.find((key) => key === "code" || key === "code_label")
      ?? (["code", "code_label"].find((key) => key in study.dimensions) ?? null)
    : null;
  const hasCodeDimension = codeDimensionKey !== null;
  const codeOptions = useMemo(() => {
    if (!study || !codeDimensionKey) return [];
    const declared = study.dimensionOptions?.[codeDimensionKey] ?? [];
    const declaredStrings = declared.flatMap((value) => typeof value === "string" ? [value] : []);
    if (declaredStrings.length > 0) return declaredStrings;
    return Array.from(new Set(study.points.flatMap((point) => {
      const value = point.dimensions[codeDimensionKey];
      return typeof value === "string" ? [value] : [];
    })));
  }, [codeDimensionKey, study]);
  const defaultCodeLabel = codeOptions.includes("bb144") ? "bb144" : codeOptions[0] ?? "";
  const activeCodeLabel = codeOptions.includes(codeLabel) ? codeLabel : defaultCodeLabel;
  const codeOptionsKey = codeOptions.join("\u0000");
  const loadingCodeOptions = Boolean(live && hasCodeDimension && registry.loadingPoints);

  useEffect(() => {
    if (loadingCodeOptions) return;
    setCodeLabel((current) => {
      if (codeOptions.includes(current)) return current;
      return defaultCodeLabel;
    });
  }, [codeOptionsKey, defaultCodeLabel, loadingCodeOptions]);

  const scopedPoints = useMemo(() => {
    if (!study) return [];
    if (!activeCodeLabel || !codeDimensionKey) return study.points;
    return study.points.filter((point) => point.dimensions[codeDimensionKey] === activeCodeLabel);
  }, [activeCodeLabel, codeDimensionKey, study]);

  const visiblePoints = useMemo(() => {
    if (!study) return [];
    const normalized = query.trim().toLocaleLowerCase();
    return scopedPoints.filter((point) => {
      if (!pointMatchesFilter(study, point, filter)) return false;
      if (!normalized) return true;
      return [
        point.displayName,
        point.canonicalKey,
        point.pointId,
        point.pointRevisionId,
        ...Object.values(point.dimensions).map(String),
      ].some((value) => value.toLocaleLowerCase().includes(normalized));
    });
  }, [filter, query, scopedPoints, study]);

  useEffect(() => {
    if (!study) return;
    setMetricKey(study.primaryMetric);
    setSelectedPoint(null);
    setPointDetailError(null);
    setFilter("all");
    setQuery("");
  }, [study?.studyId, study?.primaryMetric]);

  useEffect(() => {
    setSelectedPoint(null);
    setPointDetailError(null);
  }, [activeCodeLabel]);

  useEffect(() => {
    if (!study) return;
    try {
      sessionStorage.setItem(LAST_EXPERIMENT_STUDY_KEY, study.studyId);
    } catch {
      // The URL still retains the current study when storage is unavailable.
    }
    const url = new URL(window.location.href);
    if (live) {
      url.searchParams.delete("demo");
      url.searchParams.set("view", "experiments");
    } else {
      url.searchParams.delete("view");
      url.searchParams.set("demo", "experiments");
    }
    url.searchParams.set("study", study.studyId);
    if (!loadingCodeOptions) {
      activeCodeLabel
        ? url.searchParams.set("code", activeCodeLabel)
        : url.searchParams.delete("code");
    }
    view === "results" ? url.searchParams.delete("tab") : url.searchParams.set("tab", view);
    window.history.replaceState(null, "", url);
  }, [activeCodeLabel, live, loadingCodeOptions, study?.studyId, view]);

  if (!study) {
    const message = registry.loadingStudies
      ? "正在读取实验注册表..."
      : "当前项目还没有已发布的实验计划。";
    return (
      <div className="rr-app rr-experiment-demo">
        <main className="rr-workspace rr-exp-workspace">
          <header className="rr-page-header rr-exp-page-header">
            <div className="rr-page-title">
              <p>Remote Runner</p>
              <h1>实验结果</h1>
              <span><strong translate="no">{registry.projectId ?? "当前项目"}</strong> 的实验注册表</span>
            </div>
          </header>
          <ProductNav active="experiments" />
          {registry.error ? (
            <Alert isInline variant="danger" title="实验注册表暂不可用" className="rr-alert">
              {registry.error}
            </Alert>
          ) : <div className="rr-empty rr-exp-registry-empty">{message}</div>}
        </main>
      </div>
    );
  }

  const metric = metricByKey(study, metricKey);
  const counts = pointStatusCounts(study);
  const snapshot = isSnapshotStudy(study);
  const pointCount = study.pointCount ?? study.points.length;
  const available = availablePointCount(study);
  const activeRuns = counts.running + counts.queued;
  const needsAttention = counts.failed + counts.stale + counts.review + counts.planned;
  const coveragePercent = pointCount ? Math.round((available / pointCount) * 100) : 0;
  const scopedReviewCount = scopedPoints.filter((point) => point.status === "review").length;

  function selectPoint(point: ExperimentPoint) {
    setSelectedPoint(point);
    setPointDetailError(null);
    if (!live) return;
    void registry.loadPointDetail(study.studyId, point.pointRevisionId)
      .then((detail) => setSelectedPoint((current) => (
        current?.pointRevisionId === detail.pointRevisionId ? detail : current
      )))
      .catch((error: unknown) => {
        setPointDetailError(
          error instanceof Error ? error.message : "实验点详情读取失败",
        );
      });
  }

  async function decidePointResult(
    acceptanceId: string,
    resultId: string,
    action: "accept" | "reject",
    reason: string,
  ) {
    if (!selectedPoint) throw new Error("实验点详情已经关闭");
    await registry.decideResult(
      acceptanceId,
      selectedPoint.pointRevisionId,
      resultId,
      action,
      selectedPoint.acceptedAcceptanceId ?? null,
      reason,
    );
    setDecisionNotice(action === "accept" ? "候选结果已接受" : "候选结果已拒绝");
    setSelectedPoint(null);
    setPointDetailError(null);
    registry.refresh();
  }

  const panel = selectedPoint ? (
    <PointDetail
      study={study}
      point={selectedPoint}
      onClose={() => {
        setSelectedPoint(null);
        setPointDetailError(null);
      }}
      onDecision={live ? decidePointResult : undefined}
      detailError={pointDetailError}
    />
  ) : null;
  const projectName = live ? registry.projectId ?? "当前项目" : experimentDemoProject.displayName;
  const registryEpoch = live ? registry.registryEpoch ?? "--" : experimentDemoProject.registryEpoch;

  return (
    <div className="rr-app rr-experiment-demo">
      <Drawer isExpanded={selectedPoint !== null} position="end">
        <DrawerContent panelContent={panel}>
          <DrawerContentBody>
            <main className="rr-workspace rr-exp-workspace">
              <a className="rr-skip-link" href="#rr-exp-study-title">跳到 experiment 结果</a>
              <header className="rr-page-header rr-exp-page-header">
                <div className="rr-page-title">
                  <p>Remote Runner</p>
                  <h1>实验结果</h1>
                  <span><strong translate="no">{projectName}</strong> 的实验注册表</span>
                </div>
                <div className="rr-page-overview">
                  <div className="rr-page-status">
                    <Label isCompact color="grey" icon={<Database />}>
                      {live ? "控制器注册表 · 显式决策" : "项目快照 · 只读"}
                    </Label>
                    <span className="rr-updated rr-mono" aria-live="polite">
                      {live ? "事件" : "序列"}:{study.eventCursor} · {new Date(live ? refreshedAt : study.refreshedAt).toLocaleString("zh-CN", {
                        month: "2-digit",
                        day: "2-digit",
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit",
                        hour12: false,
                      })}
                    </span>
                    {live && (
                      <Tooltip content="刷新实验注册表">
                        <Button
                          variant="plain"
                          aria-label="刷新实验注册表"
                          onClick={() => {
                            setDecisionNotice(null);
                            registry.refresh();
                          }}
                        >
                          <RefreshCw aria-hidden="true" />
                        </Button>
                      </Tooltip>
                    )}
                  </div>
                  <div className="rr-summary rr-exp-summary" aria-label="实验摘要">
                    <div className="rr-summary-item"><span className="rr-summary-label">{snapshot ? "有记录" : "已接受"}</span><strong>{available}/{pointCount}</strong></div>
                    <div className="rr-summary-item"><span className="rr-summary-label">活跃运行</span><strong>{activeRuns}</strong></div>
                    <div className="rr-summary-item"><span className="rr-summary-label">需关注</span><strong>{needsAttention}</strong></div>
                    <div className="rr-summary-item"><span className="rr-summary-label">{snapshot ? "覆盖率" : "完成率"}</span><strong>{coveragePercent}%</strong></div>
                  </div>
                </div>
              </header>

              <ProductNav active="experiments" />

              {!live && (
                <Alert isInline variant="info" title="decoder_atomloss 项目快照" className="rr-alert">
                  基于 2026-07-27 13:10 CST 的项目文件与 Controller 序列 202 映射；不是正式 Registry acceptance，也不会写入 Controller。
                </Alert>
              )}

              {registry.error && (
                <Alert isInline variant="danger" title="实验注册表刷新失败" className="rr-alert">
                  {registry.error}
                </Alert>
              )}

              {decisionNotice && (
                <Alert isInline variant="success" title={decisionNotice} className="rr-alert">
                  Registry 已记录这次显式决策，实验点状态正在刷新。
                </Alert>
              )}

              <section className="rr-exp-revision-band" aria-labelledby="rr-exp-active-revision-title">
                <div className="rr-exp-revision-identity">
                  <span className="rr-exp-revision-icon"><GitBranch aria-hidden="true" /></span>
                  <div>
                    <p id="rr-exp-active-revision-title">{snapshot ? "项目快照版本" : "当前设计版本"}</p>
                    <strong className="rr-mono">{study.activeRevisionId}</strong>
                    {study.previousRevisionId && <small>基于 <span className="rr-mono">{study.previousRevisionId}</span></small>}
                  </div>
                </div>
                <div className="rr-exp-impact" aria-label="已发布设计影响">
                  {snapshot ? (
                    <>
                      <span><strong>{pointCount}</strong> 范围</span>
                      <span><strong>{available}</strong> 有记录</span>
                      <span><strong>{counts.review}</strong> 证据待补齐</span>
                      <span><strong>{activeRuns}</strong> 活跃</span>
                    </>
                  ) : (
                    <>
                      <span><strong>{study.impact.unchanged}</strong> 不变</span>
                      <span><strong>{study.impact.new}</strong> 新增</span>
                      <span><strong>{study.impact.stale}</strong> 过期</span>
                      <span><strong>{study.impact.archived}</strong> 归档</span>
                    </>
                  )}
                </div>
                <div className="rr-exp-revision-progress">
                  <span><strong>{coveragePercent}%</strong> {snapshot ? "当前覆盖率" : "当前接受率"}</span>
                  <span className="rr-exp-progress-track" aria-hidden="true"><span style={{ width: `${coveragePercent}%` }} /></span>
                  <small className="rr-mono">{study.planDigest.slice(0, 22)}...</small>
                </div>
              </section>

              <div className="rr-exp-layout">
                <StudyRail studies={studies} selectedStudyId={study.studyId} onSelect={setStudyId} />
                <section className="rr-exp-study-workspace" aria-labelledby="rr-exp-study-title">
                  <header className="rr-exp-study-header">
                    <div>
                      <p className="rr-eyebrow rr-mono">{study.canonicalKey}</p>
                      <h2 id="rr-exp-study-title">{study.displayName}</h2>
                      <span>{study.description}</span>
                    </div>
                    <div className="rr-exp-state-counts" aria-label="实验点状态">
                      <span><i className="rr-exp-state-complete" />{available} {snapshot ? "有记录" : "已接受"}</span>
                      <span><i className="rr-exp-state-running" />{activeRuns} 活跃</span>
                      <span><i className="rr-exp-state-attention" />{needsAttention} 需关注</span>
                    </div>
                  </header>

                  {counts.review > 0 && (
                    <Alert
                      isInline
                      variant="warning"
                      title={snapshot
                        ? activeCodeLabel
                          ? `当前 code 有 ${scopedReviewCount} 个点证据待补齐（全量 ${counts.review} 个）`
                          : `${counts.review} 个点的来源证据待补齐`
                        : `${counts.review} 个候选结果待显式接受`}
                      className="rr-exp-review-alert"
                    >
                      {snapshot
                        ? "这些点的 canonical 指标仍可查看，但 strict-convergence audit 发现 legacy per-shot 记录不完整。该状态只能在来源项目补齐证据并重新生成快照后消除，本页只读。"
                        : "候选结果已通过 eligibility 检查，但还不是当前正式结果。打开实验点详情，核对候选指标和证据后可以接受或拒绝。"}
                    </Alert>
                  )}

                  <div className={`rr-exp-toolbar ${codeOptions.length ? "rr-exp-toolbar-faceted" : ""}`} aria-label="实验筛选器">
                    {codeOptions.length > 0 && (
                      <label className="rr-exp-metric-select rr-exp-code-select">
                        <span>{codeDimensionKey ? study.dimensions[codeDimensionKey] : "Code"} <small className="rr-mono">{scopedPoints.length}/{pointCount}</small></span>
                        <select
                          aria-label="选择 Code"
                          value={activeCodeLabel}
                          onChange={(event) => setCodeLabel(event.currentTarget.value)}
                        >
                          {codeOptions.map((option) => <option key={option} value={option}>{option}</option>)}
                        </select>
                      </label>
                    )}
                    <SearchInput
                      aria-label="搜索实验点、维度或 ID"
                      name="experiment-search"
                      inputProps={{ autoComplete: "off", spellCheck: false }}
                      placeholder="搜索实验点、维度或 ID..."
                      value={query}
                      onChange={(_event, value) => setQuery(value)}
                      onClear={() => setQuery("")}
                    />
                    <ToggleGroup aria-label="实验点状态筛选">
                      {([
                        ["all", "全部"],
                        ["complete", snapshot ? "有记录" : "已接受"],
                        ["attention", snapshot ? "待补证/异常" : "待接受/异常"],
                      ] as const).map(([value, label]) => (
                        <ToggleGroupItem
                          key={value}
                          text={label}
                          buttonId={`experiment-filter-${value}`}
                          isSelected={filter === value}
                          onChange={() => setFilter(value)}
                        />
                      ))}
                    </ToggleGroup>
                    <label className="rr-exp-metric-select">
                      <span>指标</span>
                      <select value={metric.key} onChange={(event) => setMetricKey(event.currentTarget.value)}>
                        {study.metrics.map((candidate) => <option key={candidate.key} value={candidate.key}>{candidate.label}</option>)}
                      </select>
                    </label>
                  </div>

                  <Tabs className="rr-exp-tabs" activeKey={view} onSelect={(_event, key) => setView(key as ExperimentView)} aria-label="实验视图">
                    <Tab eventKey="results" title={<TabTitleText>结果</TabTitleText>} />
                    <Tab eventKey="curves" title={<TabTitleText>曲线</TabTitleText>} />
                    <Tab eventKey="matrix" title={<TabTitleText>实验点矩阵</TabTitleText>} />
                  </Tabs>

                  <div className="rr-exp-view" key={`${study.studyId}-${view}`}>
                    {live && registry.loadingPoints && study.points.length === 0 ? (
                      <div className="rr-empty">正在读取当前设计的实验点...</div>
                    ) : (
                      <>
                        {view === "results" && <ResultsTable study={study} points={visiblePoints} metric={metric} onSelect={selectPoint} />}
                        {view === "curves" && <CurveChart study={study} points={visiblePoints} metric={metric} />}
                        {view === "matrix" && <PointMatrix study={study} points={visiblePoints} metric={metric} onSelect={selectPoint} />}
                      </>
                    )}
                  </div>
                </section>
              </div>

              <footer className="rr-exp-demo-footer">
                <span><Database aria-hidden="true" />{live ? "控制器实验注册表 · 显式结果决策" : "decoder_atomloss 项目快照 · 不写入控制器"}</span>
                <span className="rr-mono">{live ? "注册表批次" : "快照批次"} {registryEpoch} · {live ? "事件" : "序列"} {live ? registry.eventCursor : study.eventCursor}</span>
              </footer>
            </main>
          </DrawerContentBody>
        </DrawerContent>
      </Drawer>
    </div>
  );
}

export function ExperimentsDemo() {
  return <ExperimentsDashboard />;
}
