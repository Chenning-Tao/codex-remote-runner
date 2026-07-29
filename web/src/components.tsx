import {
  Button,
  Checkbox,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  DrawerActions,
  DrawerCloseButton,
  DrawerHead,
  DrawerPanelBody,
  DrawerPanelContent,
  Label,
  NumberInput,
  ToggleGroup,
  ToggleGroupItem,
} from "@patternfly/react-core";
import {
  Activity,
  AlertCircle,
  ArrowDown,
  ArrowUp,
  ArrowUpToLine,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleOff,
  CirclePause,
  Gauge,
  LoaderCircle,
  Play,
  Save,
  ShieldAlert,
  CircleStop,
  WifiOff,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ageFrom,
  dateTime,
  progressLabel,
  progressValue,
  serverLoad,
  serverState,
} from "./format";
import type {
  ActiveRun,
  BatchQueueUpdateItem,
  BatchQueueUpdateChanges,
  BatchQueueUpdateResult,
  CapacityUpdateChanges,
  ConnectionState,
  DashboardDocument,
  QueueEntry,
  QueueUpdateChanges,
  Selection,
  ServerSnapshot,
} from "./types";
import { QueueUpdateError, ServerDrainError, StopRunError } from "./useDashboard";

interface StatusVisual {
  text: string;
  color: "green" | "blue" | "orange" | "red" | "grey" | "teal";
  icon: ReactNode;
}

const serverStatus: Record<string, StatusVisual> = {
  idle: { text: "空闲", color: "green", icon: <CheckCircle2 /> },
  busy: { text: "运行中", color: "teal", icon: <Activity /> },
  disabled: { text: "已禁用", color: "grey", icon: <CircleOff /> },
  unreachable: { text: "无法连接", color: "red", icon: <WifiOff /> },
  misconfigured: { text: "配置错误", color: "red", icon: <ShieldAlert /> },
  unknown: { text: "未知", color: "grey", icon: <AlertCircle /> },
};

const runStatusLabels: Record<string, string> = {
  registered: "已登记",
  queued: "排队中",
  blocked: "已阻塞",
  preparing: "准备中",
  starting: "正在启动",
  running: "运行中",
  stopping: "正在停止",
  succeeded: "已成功",
  completed: "已完成",
  failed: "失败",
  stopped: "已停止",
  canceled: "已取消",
  cancelled: "已取消",
};

function runStatusLabel(status: string | undefined): string {
  if (!status) return "运行中";
  return runStatusLabels[status] ?? status;
}

function workloadClassLabel(workloadClass: string | undefined): string {
  if (!workloadClass || workloadClass === "standard") return "标准";
  if (workloadClass === "test") return "测试";
  return workloadClass;
}

function workerPolicyLabel(workerPolicy: string | undefined): string {
  if (workerPolicy === "auto") return "自动并行";
  if (workerPolicy === "exact") return "原样执行";
  return "--";
}

function priorityLabel(priority: string | undefined): string {
  return priority === "urgent" ? "紧急" : "普通";
}

function resultIntentLabel(intent: string | undefined): string {
  if (!intent) return "--";
  return {
    candidate: "候选结果",
    supporting: "辅助结果",
    excluded: "排除",
    unclassified: "未分类",
  }[intent] ?? intent;
}

function queueWorkload(entry: QueueEntry): "standard" | "test" {
  return entry.job.workload_class === "test" ? "test" : "standard";
}

function preparedServerNames(entry: QueueEntry): string[] {
  return entry.job.supported_servers ?? entry.job.eligible_servers ?? [];
}

function serverSupportsWorkload(
  server: ServerSnapshot,
  workload: "standard" | "test",
): boolean {
  return workload === "standard"
    ? (server.standard_slots ?? 1) > 0
    : server.testing_enabled === true && (server.test_slots ?? 0) > 0;
}

function taskCanUseServer(
  entry: QueueEntry,
  server: ServerSnapshot,
  workload = queueWorkload(entry),
): boolean {
  if (!serverSupportsWorkload(server, workload)) return false;
  if (preparedServerNames(entry).includes(server.name)) return true;
  return entry.job.portable_output !== false
    && server.enabled !== false
    && !server.configuration_error
    && typeof server.configured_cores === "number"
    && server.configured_cores >= (entry.job.minimum_cores ?? 1)
    && (
      entry.job.requires_output_root !== true
      || server.output_root_configured === true
    );
}

export function ServerStatus({ server, drained = false }: { server: ServerSnapshot; drained?: boolean }) {
  const visual = serverStatus[serverState(server)] ?? serverStatus.unknown;
  return (
    <span className="rr-status-stack">
      <Label isCompact variant="outline" color={visual.color} icon={visual.icon}>
        {visual.text}
      </Label>
      {drained && <Label isCompact color="orange" icon={<CircleOff />}>暂停调度</Label>}
    </span>
  );
}

export function ConnectionStatus({
  connection,
  probeStatus,
}: {
  connection: ConnectionState;
  probeStatus?: string;
}) {
  if (connection === "reconnecting") {
    return <Label isCompact color="orange" icon={<LoaderCircle className="rr-spin" />}>正在重连</Label>;
  }
  if (connection === "connecting") {
    return <Label isCompact color="grey" icon={<LoaderCircle className="rr-spin" />}>正在连接</Label>;
  }
  if (probeStatus === "error") {
    return <Label isCompact color="red" icon={<AlertCircle />}>探测失败</Label>;
  }
  if (probeStatus === "probing") {
    return <Label isCompact color="blue" icon={<LoaderCircle className="rr-spin" />}>正在探测</Label>;
  }
  if (probeStatus !== "online") {
    return <Label isCompact color="grey" icon={<LoaderCircle className="rr-spin" />}>等待探测</Label>;
  }
  return <Label isCompact color="green" icon={<CheckCircle2 />}>控制器在线</Label>;
}

interface SummaryItemProps {
  label: string;
  value: string | number;
}

function SummaryItem({ label, value }: SummaryItemProps) {
  return (
    <div className="rr-summary-item">
      <span className="rr-summary-label">{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function SummaryStrip({ document }: { document: DashboardDocument | null }) {
  const snapshot = document?.snapshot;
  const servers = snapshot?.servers ?? [];
  const rawDrains = snapshot?.server_drains?.servers;
  const drains = new Set(Array.isArray(rawDrains) ? rawDrains : Object.keys(rawDrains ?? {}));
  const available = servers.filter((server) => ["idle", "busy"].includes(serverState(server)) && !drains.has(server.name)).length;
  const active = servers.reduce((total, server) => total + (server.active_runs?.length ?? 0), 0);
  const queued = snapshot?.summary?.queue?.active ?? snapshot?.queue?.length ?? 0;
  const sync = snapshot?.output_sync;
  const pending = typeof sync?.pending === "number" ? sync.pending : 0;

  return (
    <section className="rr-summary" aria-label="资源池摘要">
      <SummaryItem label="可用服务器" value={`${available} / ${servers.length}`} />
      <SummaryItem label="运行中" value={active} />
      <SummaryItem label="排队中" value={queued} />
      <SummaryItem label="结果同步" value={pending ? pending : "空闲"} />
    </section>
  );
}

function RunProgress({ run }: { run: ActiveRun }) {
  const value = progressValue(run.progress);
  if (value === null) return null;
  const progress = run.progress;
  const count = typeof progress?.current === "number" && typeof progress.total === "number"
    ? `${progress.current.toLocaleString()} / ${progress.total.toLocaleString()}`
    : progressLabel(run);
  return (
    <div className="rr-task-progress" aria-label={`${run.label ?? run.run_id ?? "任务"}进度`}>
      <span>{count} · 已报告 {value > 0 && value < 0.1 ? value.toFixed(2) : value.toFixed(0)}%</span>
      <span className="rr-task-progress-track" aria-hidden="true">
        <span style={{ width: `${value}%` }} />
      </span>
    </div>
  );
}

function RunEntry({
  run,
  server,
  drained,
  now,
  onSelect,
}: {
  run: ActiveRun;
  server: ServerSnapshot;
  drained: boolean;
  now: number;
  onSelect: (selection: Selection) => void;
}) {
  const elapsed = ageFrom(run.started_at, now);
  const status = run.authoritative_status ?? "running";
  return (
    <button
      type="button"
      className="rr-task-entry"
      onClick={() => onSelect({ kind: "run", value: run, server, drained })}
    >
      <span className={`rr-state-dot rr-state-${status}`} aria-hidden="true" />
      <span className="rr-task-copy">
        <span className="rr-task-title">{run.label ?? run.run_id ?? "未知任务"}</span>
        <span className="rr-task-meta">
          <span>{runStatusLabel(status)}</span>
          {elapsed !== "--" && <><span aria-hidden="true">·</span><span className="rr-mono">已运行 {elapsed}</span></>}
          <span aria-hidden="true">·</span>
          <span>{workloadClassLabel(run.workload_class)}</span>
        </span>
        <RunProgress run={run} />
      </span>
    </button>
  );
}

function loadPercent(server: ServerSnapshot): number | null {
  const cores = server.remote_cores ?? server.configured_cores;
  if (typeof server.load1 !== "number" || typeof cores !== "number" || cores <= 0) return null;
  return Math.min(100, Math.max(0, (server.load1 / cores) * 100));
}

export function ServerTable({
  servers,
  drainedServers,
  now,
  onSelect,
}: {
  servers: ServerSnapshot[];
  drainedServers: Set<string>;
  now: number;
  onSelect: (selection: Selection) => void;
}) {
  if (!servers.length) return <div className="rr-empty">没有符合当前条件的服务器。</div>;
  return (
    <div className="rr-table-scroll">
      <table className="rr-server-table">
        <caption className="rr-visually-hidden">服务器与运行中的任务</caption>
        <thead>
          <tr>
            <th scope="col">服务器</th>
            <th scope="col">硬件</th>
            <th scope="col">容量</th>
            <th scope="col">运行中的任务</th>
          </tr>
        </thead>
        <tbody>
          {servers.map((server) => {
            const runs = server.active_runs ?? [];
            const drained = drainedServers.has(server.name);
            const utilization = loadPercent(server);
            return (
              <tr key={server.name}>
                <td>
                  <button type="button" className="rr-server-cell" onClick={() => onSelect({ kind: "server", value: server, drained })}>
                    <span className={`rr-state-dot rr-server-state-${serverState(server)}`} aria-hidden="true" />
                    <span>
                      <strong translate="no">{server.name}</strong>
                      <small>{serverStatus[serverState(server)]?.text ?? "未知"}{drained ? " · 暂停调度" : ""}</small>
                    </span>
                  </button>
                </td>
                <td>
                  <div className="rr-hardware-cell">
                    <strong className="rr-mono">
                      {typeof server.configured_cores === "number" ? `${server.configured_cores} 核` : "-- 核"}
                    </strong>
                    <small className="rr-mono">
                      {typeof server.configured_memory_gb === "number" ? `${server.configured_memory_gb} GB 内存` : "-- GB 内存"}
                    </small>
                  </div>
                </td>
                <td>
                  <div className="rr-resource-cell">
                    <div className="rr-resource-line">
                      <span>1 分钟负载</span>
                      <span className="rr-mono">{serverLoad(server)}</span>
                    </div>
                    {utilization !== null && (
                      <span className="rr-resource-track" aria-label={`检测到的核心使用率为 ${utilization.toFixed(0)}%`}>
                        <span style={{ width: `${utilization}%` }} />
                      </span>
                    )}
                    <small>
                      {server.standard_runs ?? 0}/{server.standard_slots ?? 1} 标准
                      {" · "}
                      {server.test_runs ?? 0}/{server.test_slots ?? 0} 测试
                    </small>
                  </div>
                </td>
                <td>
                  {runs.length ? (
                    <div className="rr-task-list">
                      {runs.map((run) => (
                        <RunEntry key={run.run_id ?? run.label} run={run} server={server} drained={drained} now={now} onSelect={onSelect} />
                      ))}
                    </div>
                  ) : <span className="rr-no-tasks">没有运行中的任务</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      </div>
  );
}

function queuePriority(priority: string | undefined): ReactNode {
  return priority === "urgent"
    ? <span className="rr-priority rr-priority-urgent"><span aria-hidden="true" />紧急</span>
    : <span className="rr-priority rr-priority-normal"><span aria-hidden="true" />普通</span>;
}

export function QueueTable({
  entries,
  now,
  onSelect,
  onMove,
  movement,
  mutatingRunId,
  selectedRunIds,
  onToggleSelection,
  onTogglePageSelection,
}: {
  entries: QueueEntry[];
  now: number;
  onSelect: (selection: Selection) => void;
  onMove: (entry: QueueEntry, direction: "first" | "up" | "down") => void;
  movement: Map<string, { up: boolean; down: boolean }>;
  mutatingRunId: string | null;
  selectedRunIds: Set<string>;
  onToggleSelection: (entry: QueueEntry, checked: boolean) => void;
  onTogglePageSelection: (entries: QueueEntry[], checked: boolean) => void;
}) {
  if (!entries.length) return <div className="rr-empty">没有符合当前条件的排队任务。</div>;
  const selectableEntries = entries.filter((entry) => (
    entry.state.status === "queued"
    && !entry.state.placement_update
    && Boolean(entry.job.run_id)
    && typeof entry.state.revision === "number"
  ));
  const selectedOnPage = selectableEntries.filter(
    (entry) => selectedRunIds.has(entry.job.run_id!),
  ).length;
  return (
    <div className="rr-table-scroll">
      <table className="rr-queue-table">
        <caption className="rr-visually-hidden">尚未分配的任务队列</caption>
        <thead>
          <tr>
            <th scope="col" className="rr-queue-select-cell">
              <Checkbox
                id="queue-select-page"
                aria-label="选择当前页全部可编辑任务"
                isLabelWrapped
                isChecked={selectedOnPage === 0 ? false : selectedOnPage === selectableEntries.length ? true : null}
                isDisabled={!selectableEntries.length}
                onChange={(_event, checked) => onTogglePageSelection(selectableEntries, checked)}
              />
            </th>
            <th scope="col">任务</th>
            <th scope="col">优先级</th>
            <th scope="col">等待时间</th>
            <th scope="col"><span className="rr-visually-hidden">调整顺序</span></th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry, entryIndex) => {
            const runId = entry.job.run_id ?? "";
            const checkboxId = runId || `row-${entryIndex}`;
            const allowed = movement.get(runId) ?? { up: false, down: false };
            const busy = mutatingRunId !== null;
            const selectable = entry.state.status === "queued"
              && !entry.state.placement_update
              && Boolean(runId)
              && typeof entry.state.revision === "number";
            return (
            <tr key={runId || entry.job.label} aria-selected={selectedRunIds.has(runId)}>
              <td className="rr-queue-select-cell">
                <Checkbox
                  id={`queue-select-${checkboxId}`}
                  aria-label={`选择任务 ${entry.job.label ?? runId}`}
                  isLabelWrapped
                  isChecked={selectedRunIds.has(runId)}
                  isDisabled={!selectable}
                  onChange={(_event, checked) => onToggleSelection(entry, checked)}
                />
              </td>
              <td>
                <button type="button" className="rr-queue-task" onClick={() => onSelect({ kind: "queue", value: entry })}>
                  <strong>{entry.job.label ?? entry.job.run_id ?? "排队任务"}</strong>
                  <small>
                    <span>{entry.state.placement_update ? "准备服务器" : workloadClassLabel(entry.job.workload_class)}</span>
                    <span aria-hidden="true">·</span>
                    <span className="rr-mono" translate="no">{entry.job.eligible_servers?.join(", ") || "没有可用服务器"}</span>
                  </small>
                </button>
              </td>
              <td>{queuePriority(entry.job.queue_priority)}</td>
              <td className="rr-waiting rr-mono">{ageFrom(entry.job.created_at, now)}</td>
              <td>
                <div className="rr-queue-order-actions">
                  <button
                    type="button"
                    aria-label="将任务移到最前"
                    title="将任务移到最前"
                    disabled={!allowed.up || busy}
                    onClick={() => onMove(entry, "first")}
                  >
                    {mutatingRunId === runId
                      ? <LoaderCircle className="rr-spin" aria-hidden="true" />
                      : <ArrowUpToLine aria-hidden="true" />}
                  </button>
                  <button
                    type="button"
                    aria-label="上移任务"
                    title="上移任务"
                    disabled={!allowed.up || busy}
                    onClick={() => onMove(entry, "up")}
                  >
                    <ArrowUp aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    aria-label="下移任务"
                    title="下移任务"
                    disabled={!allowed.down || busy}
                    onClick={() => onMove(entry, "down")}
                  >
                    <ArrowDown aria-hidden="true" />
                  </button>
                </div>
              </td>
            </tr>
          )})}
        </tbody>
      </table>
    </div>
  );
}

type PageItem = number | "start-ellipsis" | "end-ellipsis";

function pageItems(currentPage: number, pageCount: number): PageItem[] {
  if (pageCount <= 7) {
    return Array.from({ length: pageCount }, (_value, index) => index + 1);
  }
  if (currentPage <= 4) return [1, 2, 3, 4, 5, "end-ellipsis", pageCount];
  if (currentPage >= pageCount - 3) {
    return [1, "start-ellipsis", pageCount - 4, pageCount - 3, pageCount - 2, pageCount - 1, pageCount];
  }
  return [1, "start-ellipsis", currentPage - 1, currentPage, currentPage + 1, "end-ellipsis", pageCount];
}

export function QueuePagination({
  page,
  pageCount,
  total,
  onPageChange,
}: {
  page: number;
  pageCount: number;
  total: number;
  onPageChange: (page: number) => void;
}) {
  if (pageCount <= 1) return null;
  return (
    <nav className="rr-pagination" aria-label="队列分页">
      <span className="rr-pagination-total">共 {total} 项</span>
      <div className="rr-pagination-pages">
        <button
          type="button"
          className="rr-pagination-button rr-pagination-arrow"
          aria-label="上一页"
          title="上一页"
          disabled={page === 1}
          onClick={() => onPageChange(page - 1)}
        >
          <ChevronLeft aria-hidden="true" />
        </button>
        {pageItems(page, pageCount).map((item) => typeof item === "number" ? (
          <button
            type="button"
            className={`rr-pagination-button ${item === page ? "rr-pagination-current" : ""}`}
            aria-label={`第 ${item} 页`}
            aria-current={item === page ? "page" : undefined}
            key={item}
            onClick={() => onPageChange(item)}
          >
            {item}
          </button>
        ) : (
          <span className="rr-pagination-ellipsis" aria-hidden="true" key={item}>…</span>
        ))}
        <button
          type="button"
          className="rr-pagination-button rr-pagination-arrow"
          aria-label="下一页"
          title="下一页"
          disabled={page === pageCount}
          onClick={() => onPageChange(page + 1)}
        >
          <ChevronRight aria-hidden="true" />
        </button>
      </div>
    </nav>
  );
}

function DetailGroup({ term, children, mono = false }: { term: string; children: ReactNode; mono?: boolean }) {
  return (
    <DescriptionListGroup>
      <DescriptionListTerm>{term}</DescriptionListTerm>
      <DescriptionListDescription className={mono ? "rr-mono" : undefined}>{children}</DescriptionListDescription>
    </DescriptionListGroup>
  );
}

function DetailTime({ value }: { value: string | null | undefined }) {
  if (!value || !Number.isFinite(Date.parse(value))) return <>--</>;
  return <time dateTime={value} title={value}>{dateTime(value)}</time>;
}

export function DetailPanel({
  selection,
  onClose,
  onStop,
  onQueueUpdate,
  onBatchQueueUpdate,
  onBatchResult,
  onCapacityUpdate,
  onServerDrainUpdate,
  availableServers,
}: {
  selection: Selection;
  onClose: () => void;
  onStop: (runId: string) => Promise<void>;
  onQueueUpdate: (
    runId: string,
    expectedRevision: number,
    changes: QueueUpdateChanges,
  ) => Promise<void>;
  onBatchQueueUpdate: (
    updates: BatchQueueUpdateItem[],
    changes: BatchQueueUpdateChanges,
  ) => Promise<BatchQueueUpdateResult>;
  onBatchResult: (result: BatchQueueUpdateResult) => void;
  onCapacityUpdate: (
    server: string,
    expectedRevision: number,
    changes: CapacityUpdateChanges,
  ) => Promise<void>;
  onServerDrainUpdate: (server: string, drained: boolean) => Promise<void>;
  availableServers: ServerSnapshot[];
}) {
  let title: string;
  let kind: string;
  let body: ReactNode;
  const controllerManagedRun = selection.kind !== "run"
    || (selection.value.controller_managed ?? Boolean(selection.value.authoritative_status));
  const stopRunId = selection.kind === "run"
    ? controllerManagedRun ? selection.value.run_id : undefined
    : selection.kind === "queue"
      ? selection.value.job.run_id
      : undefined;
  const [confirmingStop, setConfirmingStop] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const queueEntry = selection.kind === "queue" ? selection.value : null;
  const batchEntries = selection.kind === "queue-batch" ? selection.value : [];
  const queueRunId = queueEntry?.job.run_id;
  const batchRunKey = batchEntries.map((entry) => entry.job.run_id).join("\0");
  const [draftPriority, setDraftPriority] = useState<"urgent" | "normal">("normal");
  const [draftWorkload, setDraftWorkload] = useState<"standard" | "test">("standard");
  const [draftServers, setDraftServers] = useState<string[]>([]);
  const [savingQueue, setSavingQueue] = useState(false);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [draftBatchServers, setDraftBatchServers] = useState<string[]>([]);
  const [draftBatchPriority, setDraftBatchPriority] = useState<
    "unchanged" | "urgent" | "normal"
  >("unchanged");
  const [draftBatchWorkload, setDraftBatchWorkload] = useState<
    "unchanged" | "standard" | "test"
  >("unchanged");
  const [draftBatchServerMode, setDraftBatchServerMode] = useState<
    "unchanged" | "replace"
  >("unchanged");
  const [savingBatch, setSavingBatch] = useState(false);
  const savingBatchRef = useRef(false);
  const [activeBatchRequest, setActiveBatchRequest] = useState<{
    entryCount: number;
    preparationTotals: Record<string, number>;
    servers: string[];
  } | null>(null);
  const [batchError, setBatchError] = useState<string | null>(null);
  const selectedServer = selection.kind === "server" ? selection.value : null;
  const [draftStandardSlots, setDraftStandardSlots] = useState(1);
  const [draftTestSlots, setDraftTestSlots] = useState(0);
  const [savingCapacity, setSavingCapacity] = useState(false);
  const [capacityError, setCapacityError] = useState<string | null>(null);
  const [confirmingDrain, setConfirmingDrain] = useState(false);
  const [updatingDrain, setUpdatingDrain] = useState(false);
  const [drainError, setDrainError] = useState<string | null>(null);
  const preparedServers = queueEntry?.job.supported_servers
    ?? queueEntry?.job.eligible_servers
    ?? [];
  const preparedServerSet = new Set(preparedServers);
  const minimumCores = queueEntry?.job.minimum_cores ?? 1;
  const canPrepareAdditionalServers = queueEntry?.job.portable_output !== false;
  const preparableServers = queueEntry && canPrepareAdditionalServers
    ? availableServers.filter((server) => (
        !preparedServerSet.has(server.name)
        && server.enabled !== false
        && !server.configuration_error
        && typeof server.configured_cores === "number"
        && server.configured_cores >= minimumCores
        && (
          draftWorkload !== "test"
          || (server.testing_enabled === true && (server.test_slots ?? 0) > 0)
        )
        && (
          queueEntry.job.requires_output_root !== true
          || server.output_root_configured === true
        )
      ))
    : [];
  const serverOptions = [
    ...preparedServers,
    ...preparableServers.map((server) => server.name),
  ];
  const requiresPreparation = draftServers.some(
    (server) => !preparedServerSet.has(server),
  );
  const placementUpdating = Boolean(queueEntry?.state.placement_update);
  const supportsWorkload = (serverName: string, workload: "standard" | "test") => {
    const server = availableServers.find((item) => item.name === serverName);
    if (!server) return false;
    return workload === "standard"
      ? (server.standard_slots ?? 1) > 0
      : server.testing_enabled === true && (server.test_slots ?? 0) > 0;
  };
  const batchServerOptions = availableServers.filter((server) => (
    batchEntries.length > 0
    && batchEntries.every((entry) => taskCanUseServer(
      entry,
      server,
      draftBatchWorkload === "unchanged" ? queueWorkload(entry) : draftBatchWorkload,
    ))
  ));
  const batchServerOptionKey = batchServerOptions.map((server) => server.name).join("\0");
  const batchPreparationCounts = new Map(
    batchServerOptions.map((server) => [
      server.name,
      batchEntries.filter(
        (entry) => !preparedServerNames(entry).includes(server.name),
      ).length,
    ]),
  );
  const batchRequiresPreparation = draftBatchServers.some(
    (server) => (batchPreparationCounts.get(server) ?? 0) > 0,
  );
  const batchHasChanges = draftBatchPriority !== "unchanged"
    || draftBatchWorkload !== "unchanged"
    || draftBatchServerMode === "replace";
  const batchWorkloadNeedsServers = draftBatchWorkload !== "unchanged"
    && draftBatchServerMode === "unchanged"
    && batchEntries.some(
      (entry) => !(entry.job.eligible_servers ?? []).some((serverName) => {
        const server = availableServers.find((item) => item.name === serverName);
        return server ? serverSupportsWorkload(server, draftBatchWorkload) : false;
      }),
    );
  const activeBatchPreparationTotal = activeBatchRequest
    ? activeBatchRequest.servers.reduce(
      (total, server) => total + (activeBatchRequest.preparationTotals[server] ?? 0),
      0,
    )
    : 0;
  const activeBatchPreparationRemaining = activeBatchRequest
    ? activeBatchRequest.servers.reduce(
      (total, server) => total + Math.min(
        activeBatchRequest.preparationTotals[server] ?? 0,
        batchPreparationCounts.get(server) ?? 0,
      ),
      0,
    )
    : 0;
  const activeBatchPreparationCompleted = Math.max(
    0,
    activeBatchPreparationTotal - activeBatchPreparationRemaining,
  );
  const batchPlacementUpdating = batchEntries.some(
    (entry) => Boolean(entry.state.placement_update),
  );
  const panelBusy = stopping || savingQueue || savingCapacity || savingBatch || updatingDrain;

  useEffect(() => {
    setConfirmingStop(false);
    setStopping(false);
    setStopError(null);
  }, [stopRunId]);

  useEffect(() => {
    setDraftPriority(queueEntry?.job.queue_priority === "urgent" ? "urgent" : "normal");
    setDraftWorkload(queueEntry?.job.workload_class === "test" ? "test" : "standard");
    setDraftServers(queueEntry?.job.eligible_servers ?? []);
    setSavingQueue(false);
    setQueueError(null);
  }, [queueRunId]);

  useEffect(() => {
    if (!batchEntries.length || savingBatchRef.current) return;
    const commonServers = (batchEntries[0].job.eligible_servers ?? []).filter(
      (server) => batchEntries.every((entry) => (
        (entry.job.eligible_servers ?? []).includes(server)
        && availableServers.some(
          (option) => option.name === server && taskCanUseServer(entry, option),
        )
      )),
    );
    setDraftBatchServers(commonServers);
    setDraftBatchPriority("unchanged");
    setDraftBatchWorkload("unchanged");
    setDraftBatchServerMode("unchanged");
    setActiveBatchRequest(null);
    setBatchError(null);
  }, [batchRunKey]);

  useEffect(() => {
    if (savingBatchRef.current) return;
    const available = new Set(batchServerOptions.map((server) => server.name));
    setDraftBatchServers((current) => (
      current.filter((server) => available.has(server))
    ));
  }, [batchServerOptionKey]);

  useEffect(() => {
    setDraftStandardSlots(selectedServer?.standard_slots ?? 1);
    setDraftTestSlots(selectedServer?.test_slots ?? 0);
    setSavingCapacity(false);
    setCapacityError(null);
  }, [selectedServer?.name, selectedServer?.capacity_revision]);

  useEffect(() => {
    setConfirmingDrain(false);
    setUpdatingDrain(false);
    setDrainError(null);
  }, [selectedServer?.name, selection.kind === "server" ? selection.drained : false]);

  useEffect(() => {
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && !panelBusy) onClose();
    }
    window.document.addEventListener("keydown", closeOnEscape);
    return () => window.document.removeEventListener("keydown", closeOnEscape);
  }, [onClose, panelBusy]);

  async function stopSelectedRun() {
    if (!stopRunId || stopping) return;
    setStopping(true);
    setStopError(null);
    try {
      await onStop(stopRunId);
      onClose();
    } catch (error: unknown) {
      if (error instanceof StopRunError && error.code === "run_not_found") {
        onClose();
        return;
      }
      setStopError(error instanceof Error ? error.message : "停止任务失败");
      setConfirmingStop(false);
      setStopping(false);
    }
  }

  async function saveQueueSettings() {
    const revision = queueEntry?.state.revision;
    if (!queueRunId || typeof revision !== "number" || savingQueue) return;
    if (!draftServers.length) {
      setQueueError("请至少选择一台服务器。");
      return;
    }
    setSavingQueue(true);
    setQueueError(null);
    try {
      await onQueueUpdate(queueRunId, revision, {
        queue_priority: draftPriority,
        workload_class: draftWorkload,
        eligible_servers: draftServers,
      });
      onClose();
    } catch (error: unknown) {
      if (
        error instanceof QueueUpdateError
        && ["queue_not_found", "queue_not_editable"].includes(error.code)
      ) {
        onClose();
        return;
      }
      setQueueError(error instanceof Error ? error.message : "保存队列设置失败");
      setSavingQueue(false);
    }
  }

  async function saveBatchQueueSettings() {
    if (
      savingBatchRef.current
      || !batchHasChanges
      || batchWorkloadNeedsServers
      || (draftBatchServerMode === "replace" && !draftBatchServers.length)
    ) return;
    const updates: BatchQueueUpdateItem[] = [];
    for (const entry of batchEntries) {
      const runId = entry.job.run_id;
      const revision = entry.state.revision;
      if (!runId || typeof revision !== "number") {
        setBatchError("部分任务已无法编辑，请关闭面板后重新选择。");
        return;
      }
      updates.push({ run_id: runId, expected_revision: revision });
    }
    const changes: BatchQueueUpdateChanges = {};
    if (draftBatchPriority !== "unchanged") {
      changes.queue_priority = draftBatchPriority;
    }
    if (draftBatchWorkload !== "unchanged") {
      changes.workload_class = draftBatchWorkload;
    }
    if (draftBatchServerMode === "replace") {
      changes.eligible_servers = draftBatchServers;
    }
    setActiveBatchRequest({
      entryCount: updates.length,
      preparationTotals: Object.fromEntries(
        (changes.eligible_servers ?? []).map((server) => [
          server,
          batchPreparationCounts.get(server) ?? 0,
        ]),
      ),
      servers: [...(changes.eligible_servers ?? [])],
    });
    savingBatchRef.current = true;
    setSavingBatch(true);
    setBatchError(null);
    try {
      const result = await onBatchQueueUpdate(updates, changes);
      onBatchResult(result);
      onClose();
    } catch (error: unknown) {
      savingBatchRef.current = false;
      setActiveBatchRequest(null);
      setBatchError(error instanceof Error ? error.message : "批量修改调度设置失败");
      setSavingBatch(false);
    }
  }

  async function saveCapacitySettings() {
    const revision = selectedServer?.capacity_revision;
    if (!selectedServer || typeof revision !== "number" || savingCapacity) return;
    setSavingCapacity(true);
    setCapacityError(null);
    try {
      await onCapacityUpdate(selectedServer.name, revision, {
        standard_slots: draftStandardSlots,
        test_slots: draftTestSlots,
      });
      setSavingCapacity(false);
    } catch (error: unknown) {
      setCapacityError(error instanceof Error ? error.message : "保存服务器容量失败");
      setSavingCapacity(false);
    }
  }

  async function updateServerDrain(drained: boolean) {
    if (!selectedServer || updatingDrain) return;
    setUpdatingDrain(true);
    setDrainError(null);
    try {
      await onServerDrainUpdate(selectedServer.name, drained);
      setConfirmingDrain(false);
      setUpdatingDrain(false);
    } catch (error: unknown) {
      if (error instanceof ServerDrainError && error.code === "server_not_found") {
        onClose();
        return;
      }
      setDrainError(error instanceof Error ? error.message : "修改服务器调度状态失败");
      setUpdatingDrain(false);
    }
  }

  if (selection.kind === "server") {
    const server = selection.value;
    title = server.name;
    kind = "服务器";
    body = (
      <>
        <div className="rr-detail-status"><ServerStatus server={server} drained={selection.drained} /></div>
        <section className="rr-scheduling-editor" aria-labelledby="rr-scheduling-editor-title">
          <h3 id="rr-scheduling-editor-title">调度控制</h3>
          <p>
            {selection.drained
              ? "已暂停所有项目向这台服务器分配新任务；现有任务不受影响。"
              : "允许控制器从所有项目向这台服务器分配新任务。"}
          </p>
          {drainError && <div className="rr-stop-error" role="alert">{drainError}</div>}
          {selection.drained ? (
            <Button
              variant="primary"
              icon={updatingDrain ? <LoaderCircle className="rr-spin" /> : <Play />}
              isDisabled={panelBusy}
              onClick={() => updateServerDrain(false)}
            >
              {updatingDrain ? "正在恢复…" : "恢复调度"}
            </Button>
          ) : confirmingDrain ? (
            <div className="rr-scheduling-confirm" role="group" aria-label="确认暂停服务器调度">
              <strong>确认暂停所有项目的新任务调度？</strong>
              <span>已在运行的任务会继续执行。</span>
              <div>
                <Button
                  variant="danger"
                  icon={updatingDrain ? <LoaderCircle className="rr-spin" /> : <CirclePause />}
                  isDisabled={panelBusy}
                  onClick={() => updateServerDrain(true)}
                >
                  {updatingDrain ? "正在暂停…" : "确认暂停"}
                </Button>
                <Button variant="link" isDisabled={panelBusy} onClick={() => setConfirmingDrain(false)}>
                  取消
                </Button>
              </div>
            </div>
          ) : (
            <Button
              variant="secondary"
              icon={<CirclePause />}
              isDisabled={panelBusy}
              onClick={() => setConfirmingDrain(true)}
            >
              暂停调度
            </Button>
          )}
        </section>
        <section className="rr-capacity-editor" aria-labelledby="rr-capacity-editor-title">
          <h3 id="rr-capacity-editor-title">并发任务容量</h3>
          <div className="rr-capacity-fields">
            <label htmlFor="rr-standard-slots">Standard</label>
            <NumberInput
              value={draftStandardSlots}
              min={0}
              max={1024}
              inputName="rr-standard-slots"
              inputProps={{ id: "rr-standard-slots", inputMode: "numeric" }}
              inputAriaLabel="Standard 并发任务数"
              minusBtnAriaLabel="减少 Standard 并发任务数"
              plusBtnAriaLabel="增加 Standard 并发任务数"
              onMinus={() => setDraftStandardSlots((value) => Math.max(0, value - 1))}
              onPlus={() => setDraftStandardSlots((value) => Math.min(1024, value + 1))}
              onChange={(event) => {
                const value = Number.parseInt(event.currentTarget.value, 10);
                if (Number.isFinite(value)) setDraftStandardSlots(Math.min(1024, Math.max(0, value)));
              }}
            />
            <label htmlFor="rr-test-slots">Test</label>
            <NumberInput
              value={draftTestSlots}
              min={0}
              max={1024}
              inputName="rr-test-slots"
              inputProps={{ id: "rr-test-slots", inputMode: "numeric" }}
              inputAriaLabel="Test 并发任务数"
              minusBtnAriaLabel="减少 Test 并发任务数"
              plusBtnAriaLabel="增加 Test 并发任务数"
              onMinus={() => setDraftTestSlots((value) => Math.max(0, value - 1))}
              onPlus={() => setDraftTestSlots((value) => Math.min(1024, value + 1))}
              onChange={(event) => {
                const value = Number.parseInt(event.currentTarget.value, 10);
                if (Number.isFinite(value)) setDraftTestSlots(Math.min(1024, Math.max(0, value)));
              }}
            />
          </div>
          {capacityError && <div className="rr-stop-error" role="alert">{capacityError}</div>}
          <Button
            variant="primary"
            icon={savingCapacity ? <LoaderCircle className="rr-spin" /> : <Save />}
            isDisabled={panelBusy || typeof server.capacity_revision !== "number"}
            onClick={saveCapacitySettings}
          >
            {savingCapacity ? "正在保存…" : "保存容量"}
          </Button>
        </section>
        <DescriptionList isHorizontal>
          <DetailGroup term="负载（1 / 5 / 15 分钟）" mono>{[server.load1, server.load5, server.load15].map((value) => typeof value === "number" ? value.toFixed(1) : "--").join(" / ")}</DetailGroup>
          <DetailGroup term="配置核心数" mono>{server.configured_cores ?? "--"}</DetailGroup>
          <DetailGroup term="配置内存" mono>{typeof server.configured_memory_gb === "number" ? `${server.configured_memory_gb} GB` : "--"}</DetailGroup>
          <DetailGroup term="远程核心数" mono>{server.remote_cores ?? "--"}</DetailGroup>
          <DetailGroup term="Standard 槽位" mono>{server.standard_slots ?? 1}</DetailGroup>
          <DetailGroup term="Test 槽位" mono>{server.test_slots ?? 0}</DetailGroup>
          <DetailGroup term="自动分配">{server.auto_select === false ? "已排除" : "可分配"}</DetailGroup>
          <DetailGroup term="调度状态">{selection.drained ? "暂停调度" : "正常调度"}</DetailGroup>
          {(server.configuration_error || server.error) && <DetailGroup term="错误">{server.configuration_error ?? server.error}</DetailGroup>}
        </DescriptionList>
      </>
    );
  } else if (selection.kind === "run") {
    const run = selection.value;
    title = run.label ?? run.run_id ?? "运行中的任务";
    kind = "运行中的任务";
    body = (
      <>
        {!controllerManagedRun && (
          <div className="rr-unmanaged-notice" role="status">
            <AlertCircle aria-hidden="true" />
            <div>
              <strong>控制器未登记这个任务</strong>
              <span>服务器仍检测到任务进程，因此继续显示；当前无法从网页停止。</span>
            </div>
          </div>
        )}
        <DescriptionList isHorizontal>
          <DetailGroup term="运行 ID" mono>{run.run_id ?? "--"}</DetailGroup>
          <DetailGroup term="任务 ID" mono>{run.task_id ?? "--"}</DetailGroup>
          <DetailGroup term="服务器" mono>{selection.server.name}</DetailGroup>
          <DetailGroup term="管理状态">{controllerManagedRun ? "由控制器管理" : "未在控制器中登记"}</DetailGroup>
          <DetailGroup term="调度状态">{selection.drained ? "暂停调度" : "正常调度"}</DetailGroup>
          <DetailGroup term="任务类型">{workloadClassLabel(run.workload_class)}</DetailGroup>
          <DetailGroup term="状态">{runStatusLabel(run.authoritative_status)}</DetailGroup>
          <DetailGroup term="开始时间"><DetailTime value={run.started_at} /></DetailGroup>
          {run.error && <DetailGroup term="错误">{run.error}</DetailGroup>}
        </DescriptionList>
      </>
    );
  } else if (selection.kind === "queue") {
    const entry = selection.value;
    title = entry.job.label ?? entry.job.run_id ?? "排队任务";
    kind = "排队任务";
    body = (
      <>
        <section className="rr-queue-editor" aria-labelledby="rr-queue-editor-title">
          <h3 id="rr-queue-editor-title">调度设置</h3>
          <div className="rr-queue-editor-field">
            <span>任务类型</span>
            <ToggleGroup aria-label="任务类型">
              <ToggleGroupItem
                text="Standard"
                buttonId="queue-workload-standard"
                isSelected={draftWorkload === "standard"}
                onChange={() => {
                  setDraftWorkload("standard");
                  setDraftServers((current) => current.filter((name) => supportsWorkload(name, "standard")));
                }}
              />
              <ToggleGroupItem
                text="Test"
                buttonId="queue-workload-test"
                isSelected={draftWorkload === "test"}
                onChange={() => {
                  setDraftWorkload("test");
                  setDraftServers((current) => current.filter((name) => supportsWorkload(name, "test")));
                }}
              />
            </ToggleGroup>
          </div>
          <div className="rr-queue-editor-field">
            <span>优先级</span>
            <ToggleGroup aria-label="任务优先级">
              <ToggleGroupItem
                text="紧急"
                buttonId="queue-priority-urgent"
                isSelected={draftPriority === "urgent"}
                onChange={() => setDraftPriority("urgent")}
              />
              <ToggleGroupItem
                text="普通"
                buttonId="queue-priority-normal"
                isSelected={draftPriority === "normal"}
                onChange={() => setDraftPriority("normal")}
              />
            </ToggleGroup>
          </div>
          <fieldset className="rr-server-options">
            <legend>支持的服务器</legend>
            {serverOptions.map((server) => (
              <Checkbox
                key={server}
                id={`queue-server-${server}`}
                label={(
                  <span className="rr-server-option-label">
                    <span>{server}</span>
                    {!preparedServerSet.has(server) && <small>需准备</small>}
                    {!supportsWorkload(server, draftWorkload) && <small>通道关闭</small>}
                  </span>
                )}
                isChecked={draftServers.includes(server)}
                isDisabled={!supportsWorkload(server, draftWorkload)}
                onChange={(_event, checked) => {
                  setDraftServers((current) => checked
                    ? [...current, server]
                    : current.filter((name) => name !== server));
                }}
              />
            ))}
          </fieldset>
          {queueError && <div className="rr-stop-error" role="alert">{queueError}</div>}
          <Button
            variant="primary"
            icon={savingQueue ? <LoaderCircle className="rr-spin" /> : <Save />}
            isDisabled={savingQueue || placementUpdating || !draftServers.length}
            onClick={saveQueueSettings}
          >
            {savingQueue
              ? requiresPreparation ? "正在准备…" : "正在保存…"
              : requiresPreparation ? "准备并保存" : "保存设置"}
          </Button>
        </section>
        <DescriptionList isHorizontal>
          <DetailGroup term="运行 ID" mono>{entry.job.run_id ?? "--"}</DetailGroup>
          <DetailGroup term="任务 ID" mono>{entry.job.task_id ?? "--"}</DetailGroup>
          <DetailGroup term="当前优先级">{priorityLabel(entry.job.queue_priority)}</DetailGroup>
          <DetailGroup term="任务类型">{workloadClassLabel(entry.job.workload_class)}</DetailGroup>
          <DetailGroup term="并行策略">{workerPolicyLabel(entry.job.worker_policy)}</DetailGroup>
          <DetailGroup term="结果处理方式">{resultIntentLabel(entry.job.result_intent)}</DetailGroup>
          <DetailGroup term="当前服务器">{entry.job.eligible_servers?.join(", ") || "无"}</DetailGroup>
          <DetailGroup term="状态">{runStatusLabel(entry.state.status ?? "queued")}</DetailGroup>
          {entry.state.placement_update && (
            <DetailGroup term="队列更新">正在准备 {entry.state.placement_update.requested_servers?.join(", ")}</DetailGroup>
          )}
          <DetailGroup term="创建时间"><DetailTime value={entry.job.created_at} /></DetailGroup>
          {entry.state.error && <DetailGroup term="错误">{entry.state.error}</DetailGroup>}
        </DescriptionList>
      </>
    );
  } else {
    title = `${activeBatchRequest?.entryCount ?? batchEntries.length} 项排队任务`;
    kind = "批量调度设置";
    body = (
      <section className="rr-queue-editor" aria-labelledby="rr-batch-server-editor-title">
        <h3 id="rr-batch-server-editor-title">统一调度设置</h3>
        <p className="rr-batch-editor-copy">
          只会覆盖明确选择的设置。各任务独立更新，期间可能有部分任务失败。
        </p>
        <div className="rr-queue-editor-field">
          <span>任务类型</span>
          <ToggleGroup aria-label="批量任务类型">
            <ToggleGroupItem
              text="保持不变"
              buttonId="queue-batch-workload-unchanged"
              isSelected={draftBatchWorkload === "unchanged"}
              onChange={() => setDraftBatchWorkload("unchanged")}
            />
            <ToggleGroupItem
              text="Standard"
              buttonId="queue-batch-workload-standard"
              isSelected={draftBatchWorkload === "standard"}
              onChange={() => setDraftBatchWorkload("standard")}
            />
            <ToggleGroupItem
              text="Test"
              buttonId="queue-batch-workload-test"
              isSelected={draftBatchWorkload === "test"}
              onChange={() => setDraftBatchWorkload("test")}
            />
          </ToggleGroup>
        </div>
        <div className="rr-queue-editor-field">
          <span>优先级</span>
          <ToggleGroup aria-label="批量任务优先级">
            <ToggleGroupItem
              text="保持不变"
              buttonId="queue-batch-priority-unchanged"
              isSelected={draftBatchPriority === "unchanged"}
              onChange={() => setDraftBatchPriority("unchanged")}
            />
            <ToggleGroupItem
              text="紧急"
              buttonId="queue-batch-priority-urgent"
              isSelected={draftBatchPriority === "urgent"}
              onChange={() => setDraftBatchPriority("urgent")}
            />
            <ToggleGroupItem
              text="普通"
              buttonId="queue-batch-priority-normal"
              isSelected={draftBatchPriority === "normal"}
              onChange={() => setDraftBatchPriority("normal")}
            />
          </ToggleGroup>
        </div>
        <div className="rr-queue-editor-field">
          <span>可用服务器</span>
          <ToggleGroup aria-label="批量服务器设置方式">
            <ToggleGroupItem
              text="保持不变"
              buttonId="queue-batch-servers-unchanged"
              isSelected={draftBatchServerMode === "unchanged"}
              onChange={() => setDraftBatchServerMode("unchanged")}
            />
            <ToggleGroupItem
              text="统一设置"
              buttonId="queue-batch-servers-replace"
              isSelected={draftBatchServerMode === "replace"}
              onChange={() => setDraftBatchServerMode("replace")}
            />
          </ToggleGroup>
        </div>
        {draftBatchServerMode === "replace" && (
          <fieldset className="rr-server-options">
            <legend>支持的服务器</legend>
            {batchServerOptions.length ? batchServerOptions.map((server) => {
              const preparationCount = batchPreparationCounts.get(server.name) ?? 0;
              const preparationTotal = activeBatchRequest?.preparationTotals[server.name] ?? 0;
              const preparationCompleted = Math.max(
                0,
                preparationTotal - Math.min(preparationTotal, preparationCount),
              );
              return (
                <Checkbox
                  key={server.name}
                  id={`queue-batch-server-${server.name}`}
                  label={(
                    <span className="rr-server-option-label">
                      <span>{server.name}</span>
                      {savingBatch && preparationTotal > 0
                        ? (
                          <small>
                            {preparationCompleted === preparationTotal
                              ? "准备完成"
                              : `已准备 ${preparationCompleted} / ${preparationTotal}`}
                          </small>
                        )
                        : preparationCount > 0 && <small>需准备 {preparationCount} 项</small>}
                    </span>
                  )}
                  isChecked={draftBatchServers.includes(server.name)}
                  isDisabled={savingBatch}
                  onChange={(_event, checked) => {
                    setDraftBatchServers((current) => checked
                      ? [...current, server.name]
                      : current.filter((name) => name !== server.name));
                  }}
                />
              );
            }) : (
              <div className="rr-batch-empty" role="status">
                没有同时满足全部所选任务要求的服务器。
              </div>
            )}
          </fieldset>
        )}
        {batchWorkloadNeedsServers && (
          <div className="rr-stop-error" role="alert">
            部分任务的当前服务器未开通目标通道，请同时统一设置可用服务器。
          </div>
        )}
        {batchError && <div className="rr-stop-error" role="alert">{batchError}</div>}
        <Button
          variant="primary"
          icon={savingBatch ? <LoaderCircle className="rr-spin" /> : <Save />}
          isDisabled={
            savingBatch
            || batchPlacementUpdating
            || !batchHasChanges
            || batchWorkloadNeedsServers
            || (draftBatchServerMode === "replace" && !draftBatchServers.length)
          }
          onClick={saveBatchQueueSettings}
        >
          {savingBatch
            ? activeBatchPreparationTotal > 0
              ? activeBatchPreparationRemaining > 0
                ? `正在准备 ${activeBatchPreparationCompleted} / ${activeBatchPreparationTotal}…`
                : "准备完成，正在应用…"
              : "正在批量应用…"
            : draftBatchServerMode === "replace" && batchRequiresPreparation
              ? "准备并应用到全部"
              : "应用到全部"}
        </Button>
      </section>
    );
  }

  return (
    <DrawerPanelContent className="rr-detail-panel" focusTrap={{ enabled: true, "aria-labelledby": "rr-detail-title" }}>
      <DrawerHead>
        <div>
          <p className="rr-eyebrow">{kind}</p>
          <h2 id="rr-detail-title">{title}</h2>
        </div>
        <DrawerActions>
          {!panelBusy && <DrawerCloseButton onClose={onClose} aria-label="关闭详情" />}
        </DrawerActions>
      </DrawerHead>
      <DrawerPanelBody>{body}</DrawerPanelBody>
      {stopRunId && (
        <div className="rr-detail-actions">
          {stopError && <div className="rr-stop-error" role="alert">{stopError}</div>}
          {confirmingStop ? (
            <div className="rr-stop-confirmation">
              <div>
                <strong>确认停止这个任务？</strong>
                <span>停止后不会自动恢复。</span>
              </div>
              <div className="rr-stop-buttons">
                <Button
                  variant="danger"
                  icon={stopping ? <LoaderCircle className="rr-spin" /> : <CircleStop />}
                  isDisabled={stopping}
                  onClick={stopSelectedRun}
                >
                  {stopping ? "正在停止…" : "确认停止"}
                </Button>
                <Button
                  variant="secondary"
                  isDisabled={stopping}
                  onClick={() => setConfirmingStop(false)}
                >
                  取消
                </Button>
              </div>
            </div>
          ) : (
            <Button
              variant="danger"
              icon={<CircleStop />}
              onClick={() => setConfirmingStop(true)}
            >
              停止任务
            </Button>
          )}
        </div>
      )}
    </DrawerPanelContent>
  );
}

export function SnapshotHealth({ document, now }: { document: DashboardDocument | null; now: number }) {
  const age = ageFrom(document?.refreshed_at, now);
  const stale = document?.refreshed_at
    ? now - Date.parse(document.refreshed_at) > document.probe_interval_seconds * 2 * 1000
    : false;
  return (
    <div className="rr-snapshot-health">
      <span><Gauge aria-hidden="true" />快照{document?.snapshot ? (stale ? "已过期" : "正常") : "等待中"}</span>
      <span className="rr-mono">快照年龄 {age}</span>
      <span className="rr-mono">探测间隔 {document?.probe_interval_seconds ?? "--"}秒</span>
    </div>
  );
}
