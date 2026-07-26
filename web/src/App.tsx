import {
  Alert,
  Button,
  Drawer,
  DrawerContent,
  DrawerContentBody,
  SearchInput,
  Skeleton,
  Tab,
  Tabs,
  TabTitleText,
  ToggleGroup,
  ToggleGroupItem,
  Tooltip,
} from "@patternfly/react-core";
import { RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  ConnectionStatus,
  DetailPanel,
  QueuePagination,
  QueueTable,
  ServerTable,
  SnapshotHealth,
  SummaryStrip,
} from "./components";
import { ageFrom, serverState, textMatches } from "./format";
import type { QueueEntry, Selection, ServerSnapshot } from "./types";
import { useDashboard } from "./useDashboard";

type PriorityFilter = "all" | "urgent" | "normal";
type MobileView = "servers" | "queue";
const QUEUE_PAGE_SIZE = 20;

const priorityLabels: Record<PriorityFilter, string> = {
  all: "全部",
  urgent: "紧急",
  normal: "普通",
};

function initialPriority(): PriorityFilter {
  const value = new URLSearchParams(window.location.search).get("priority");
  return value === "urgent" || value === "normal" ? value : "all";
}

function initialMobileView(): MobileView {
  return new URLSearchParams(window.location.search).get("view") === "queue" ? "queue" : "servers";
}

function initialQueuePage(): number {
  const value = Number.parseInt(new URLSearchParams(window.location.search).get("page") ?? "", 10);
  return Number.isFinite(value) && value > 0 ? value : 1;
}

function serverMatches(server: ServerSnapshot, query: string): boolean {
  return textMatches(
    [
      server.name,
      serverState(server),
      server.configuration_error ?? undefined,
      ...(server.active_runs ?? []).flatMap((run) => [run.label, run.run_id, run.task_id]),
    ],
    query,
  );
}

function queueMatches(entry: QueueEntry, query: string): boolean {
  return textMatches(
    [
      entry.job.label,
      entry.job.run_id,
      entry.job.task_id,
      entry.job.workload_class,
      entry.job.queue_priority,
      entry.state.status,
      ...(entry.job.eligible_servers ?? []),
    ],
    query,
  );
}

export default function App() {
  const {
    document,
    connection,
    initialError,
    reconnect,
    stopRun,
    updateQueue,
    updateCapacity,
  } = useDashboard();
  const [query, setQuery] = useState(() => new URLSearchParams(window.location.search).get("q") ?? "");
  const [priority, setPriority] = useState<PriorityFilter>(initialPriority);
  const [mobileView, setMobileView] = useState<MobileView>(initialMobileView);
  const [queuePage, setQueuePage] = useState(initialQueuePage);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [now, setNow] = useState(Date.now());
  const [mutatingRunId, setMutatingRunId] = useState<string | null>(null);
  const [queueActionError, setQueueActionError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const url = new URL(window.location.href);
    query ? url.searchParams.set("q", query) : url.searchParams.delete("q");
    priority === "all" ? url.searchParams.delete("priority") : url.searchParams.set("priority", priority);
    mobileView === "servers" ? url.searchParams.delete("view") : url.searchParams.set("view", mobileView);
    queuePage === 1 ? url.searchParams.delete("page") : url.searchParams.set("page", String(queuePage));
    window.history.replaceState(null, "", url);
  }, [mobileView, priority, query, queuePage]);

  const servers = useMemo(
    () => (document?.snapshot?.servers ?? []).filter((server) => serverMatches(server, query.trim())),
    [document, query],
  );
  const queue = useMemo(
    () => (document?.snapshot?.queue ?? []).filter((entry) => {
      const entryPriority = entry.job.queue_priority ?? "normal";
      const matchesPriority = priority === "all" || entryPriority === priority;
      return matchesPriority && queueMatches(entry, query.trim());
    }),
    [document, priority, query],
  );
  const serverTotal = document?.snapshot?.servers?.length ?? 0;
  const queuedTotal = document?.snapshot?.summary?.queue?.active ?? document?.snapshot?.queue?.length ?? 0;
  const queueOmitted = document?.snapshot?.summary?.queue?.omitted ?? 0;
  const serverCount = query.trim() && serverTotal > servers.length
    ? `${servers.length} / ${serverTotal} 台服务器`
    : `${servers.length} 台服务器`;
  const queueCount = queuedTotal > queue.length
    ? `${queue.length} / ${queuedTotal} 项任务`
    : `${queue.length} 项任务`;
  const queuePageCount = Math.max(1, Math.ceil(queue.length / QUEUE_PAGE_SIZE));
  const currentQueuePage = Math.min(queuePage, queuePageCount);
  const paginatedQueue = queue.slice(
    (currentQueuePage - 1) * QUEUE_PAGE_SIZE,
    currentQueuePage * QUEUE_PAGE_SIZE,
  );
  const queueMovement = useMemo(() => {
    const result = new Map<string, { up: boolean; down: boolean }>();
    const lanes = new Map<string, QueueEntry[]>();
    for (const entry of document?.snapshot?.queue ?? []) {
      if (
        entry.state.status !== "queued"
        || entry.state.placement_update
        || !entry.job.run_id
      ) continue;
      const key = `${entry.job.queue_priority ?? "normal"}\0${entry.job.workload_class ?? "standard"}`;
      const lane = lanes.get(key) ?? [];
      lane.push(entry);
      lanes.set(key, lane);
    }
    for (const lane of lanes.values()) {
      lane.forEach((entry, index) => {
        result.set(entry.job.run_id!, {
          up: index > 0,
          down: index < lane.length - 1,
        });
      });
    }
    return result;
  }, [document]);
  const drainedServers = useMemo(() => {
    const raw = document?.snapshot?.server_drains?.servers;
    return new Set(Array.isArray(raw) ? raw : Object.keys(raw ?? {}));
  }, [document]);

  useEffect(() => {
    if (document && queuePage !== currentQueuePage) setQueuePage(currentQueuePage);
  }, [currentQueuePage, document, queuePage]);

  useEffect(() => {
    setSelection((current) => {
      if (current?.kind === "server") {
        const refreshed = document?.snapshot?.servers?.find(
          (server) => server.name === current.value.name,
        );
        return refreshed
          ? { kind: "server", value: refreshed, drained: current.drained }
          : null;
      }
      if (current?.kind !== "queue") return current;
      const runId = current.value.job.run_id;
      const refreshed = document?.snapshot?.queue?.find(
        (entry) => entry.job.run_id === runId,
      );
      return refreshed ? { kind: "queue", value: refreshed } : null;
    });
  }, [document]);

  function changeQueuePage(page: number) {
    setQueuePage(page);
    window.requestAnimationFrame(() => {
      window.document.getElementById("queue-title")?.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start",
      });
    });
  }

  async function moveQueueEntry(entry: QueueEntry, direction: "up" | "down") {
    const runId = entry.job.run_id;
    const revision = entry.state.revision;
    if (!runId || typeof revision !== "number" || mutatingRunId) return;
    setMutatingRunId(runId);
    setQueueActionError(null);
    try {
      await updateQueue(runId, revision, { move: direction });
    } catch (error: unknown) {
      setQueueActionError(error instanceof Error ? error.message : "调整队列顺序失败");
    } finally {
      setMutatingRunId(null);
    }
  }

  const panel = selection ? (
    <DetailPanel
      selection={selection}
      onClose={() => setSelection(null)}
      onStop={stopRun}
      onQueueUpdate={updateQueue}
      onCapacityUpdate={updateCapacity}
      availableServers={document?.snapshot?.servers ?? []}
    />
  ) : null;

  return (
    <div className="rr-app">
      <Drawer isExpanded={selection !== null} position="end">
        <DrawerContent panelContent={panel}>
          <DrawerContentBody>
            <main className="rr-workspace">
              <a className="rr-skip-link" href="#servers-title">跳到服务器列表</a>
              {(initialError || document?.error) && (
                <Alert
                  isInline
                  variant="danger"
                  title={document?.snapshot ? "最新探测失败，当前显示上一次成功的快照" : "仪表盘暂不可用"}
                  className="rr-alert"
                >
                  {document?.error ?? initialError}
                </Alert>
              )}

              <header className="rr-page-header">
                <div className="rr-page-title">
                  <p>Remote Runner</p>
                  <h1>远程任务</h1>
                  <span><strong translate="no">{document?.project_id ?? "当前项目"}</strong> 的实时控制器状态</span>
                </div>
                <div className="rr-page-overview">
                  <div className="rr-page-status">
                    <span className="rr-connection-live" aria-live="polite">
                      <ConnectionStatus connection={connection} probeStatus={document?.status} />
                    </span>
                    <span className="rr-updated rr-mono">
                      {document?.refreshed_at ? `${ageFrom(document.refreshed_at, now)}前更新` : "正在等待快照…"}
                    </span>
                    <Tooltip content="重新连接仪表盘数据流">
                      <Button variant="plain" aria-label="重新连接仪表盘数据流" onClick={reconnect}>
                        <RefreshCw aria-hidden="true" />
                      </Button>
                    </Tooltip>
                  </div>
                  <SummaryStrip document={document} />
                </div>
              </header>

              <div className="rr-toolbar" aria-label="仪表盘筛选">
                <SearchInput
                  aria-label="搜索服务器、任务或运行 ID"
                  name="dashboard-search"
                  inputProps={{ autoComplete: "off", spellCheck: false }}
                  placeholder="搜索服务器、任务或运行 ID…"
                  value={query}
                  onChange={(_event, value) => {
                    setQuery(value);
                    setQueuePage(1);
                  }}
                  onClear={() => {
                    setQuery("");
                    setQueuePage(1);
                  }}
                />
                <ToggleGroup aria-label="队列优先级筛选">
                  {(["all", "urgent", "normal"] as const).map((value) => (
                    <ToggleGroupItem
                      key={value}
                      text={priorityLabels[value]}
                      buttonId={`priority-${value}`}
                      isSelected={priority === value}
                      onChange={() => {
                        setPriority(value);
                        setQueuePage(1);
                      }}
                    />
                  ))}
                </ToggleGroup>
              </div>

              <Tabs
                className="rr-mobile-tabs"
                activeKey={mobileView}
                onSelect={(_event, eventKey) => setMobileView(eventKey as MobileView)}
                aria-label="仪表盘数据表"
                isFilled
              >
                <Tab eventKey="servers" title={<TabTitleText>服务器</TabTitleText>} />
                <Tab eventKey="queue" title={<TabTitleText>队列</TabTitleText>} />
              </Tabs>

              <div className="rr-operations-grid">
                <section className={`rr-data-section rr-server-section ${mobileView !== "servers" ? "rr-mobile-hidden" : ""}`} aria-labelledby="servers-title">
                  <header className="rr-section-header">
                    <div>
                      <h2 id="servers-title">服务器</h2>
                      <p>计算资源与当前任务</p>
                    </div>
                    <span className="rr-count rr-mono">{serverCount}</span>
                  </header>
                  {!document ? (
                    <div className="rr-loading" aria-label="正在加载服务器"><Skeleton width="34%" /><Skeleton width="100%" /><Skeleton width="100%" /></div>
                  ) : <ServerTable servers={servers} drainedServers={drainedServers} now={now} onSelect={setSelection} />}
                </section>

                <section className={`rr-data-section rr-queue-section ${mobileView !== "queue" ? "rr-mobile-hidden" : ""}`} aria-labelledby="queue-title">
                  <header className="rr-section-header">
                    <div>
                      <h2 id="queue-title">队列</h2>
                      <p>等待分配服务器的任务</p>
                    </div>
                    <span className="rr-count rr-mono">{queueCount}</span>
                  </header>
                  {!document ? (
                    <div className="rr-loading" aria-label="正在加载队列"><Skeleton width="27%" /><Skeleton width="100%" /></div>
                  ) : (
                    <>
                      {queueOmitted > 0 && (
                        <div className="rr-queue-limit-notice" role="status">
                          当前控制器仅返回 {queue.length} / {queuedTotal} 项任务；升级控制器后可查看全部分页。
                        </div>
                      )}
                      {queueActionError && (
                        <Alert
                          isInline
                          variant="danger"
                          title="队列顺序修改失败"
                          className="rr-queue-action-alert"
                        >
                          {queueActionError}
                        </Alert>
                      )}
                      <QueueTable
                        entries={paginatedQueue}
                        now={now}
                        onSelect={setSelection}
                        onMove={moveQueueEntry}
                        movement={queueMovement}
                        mutatingRunId={mutatingRunId}
                      />
                      <QueuePagination
                        page={currentQueuePage}
                        pageCount={queuePageCount}
                        total={queue.length}
                        onPageChange={changeQueuePage}
                      />
                    </>
                  )}
                </section>
              </div>

              <SnapshotHealth document={document} now={now} />
            </main>
          </DrawerContentBody>
        </DrawerContent>
      </Drawer>
    </div>
  );
}
