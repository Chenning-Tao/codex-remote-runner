import {
  Alert,
  Button,
  Drawer,
  DrawerContent,
  DrawerContentBody,
  Masthead,
  MastheadBrand,
  MastheadContent,
  MastheadMain,
  SearchInput,
  Skeleton,
  Tab,
  Tabs,
  TabTitleText,
  ToggleGroup,
  ToggleGroupItem,
  Tooltip,
} from "@patternfly/react-core";
import { RefreshCw, TerminalSquare } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  ConnectionStatus,
  DetailPanel,
  QueueTable,
  ServerTable,
  SnapshotHealth,
  SummaryStrip,
} from "./components";
import { serverState, textMatches } from "./format";
import type { QueueEntry, Selection, ServerSnapshot } from "./types";
import { useDashboard } from "./useDashboard";

type PriorityFilter = "all" | "urgent" | "normal";
type MobileView = "servers" | "queue";

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
  const { document, connection, initialError, reconnect } = useDashboard();
  const [query, setQuery] = useState("");
  const [priority, setPriority] = useState<PriorityFilter>("all");
  const [mobileView, setMobileView] = useState<MobileView>("servers");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

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
  const serverCount = query.trim() && serverTotal > servers.length
    ? `${servers.length} / ${serverTotal} servers`
    : `${servers.length} servers`;
  const queueCount = queuedTotal > queue.length
    ? `${queue.length} / ${queuedTotal} jobs`
    : `${queue.length} jobs`;
  const drainedServers = useMemo(() => {
    const raw = document?.snapshot?.server_drains?.servers;
    return new Set(Array.isArray(raw) ? raw : Object.keys(raw ?? {}));
  }, [document]);

  const panel = selection ? <DetailPanel selection={selection} onClose={() => setSelection(null)} /> : null;

  return (
    <div className="rr-app">
      <Masthead className="rr-masthead" display={{ default: "inline" }}>
        <MastheadMain>
          <MastheadBrand>
            <div className="rr-brand-mark" aria-hidden="true"><TerminalSquare /></div>
            <div className="rr-brand-copy">
              <span className="rr-brand-name">Remote Runner</span>
              <span className="rr-project rr-mono">{document?.project_id ?? "loading project"}</span>
            </div>
          </MastheadBrand>
        </MastheadMain>
        <MastheadContent>
          <div className="rr-masthead-status">
            <ConnectionStatus connection={connection} probeStatus={document?.status} />
            <span className="rr-updated rr-mono">
              {document?.refreshed_at ? `updated ${Math.max(0, Math.floor((now - Date.parse(document.refreshed_at)) / 1000))}s ago` : "waiting for snapshot"}
            </span>
            <Tooltip content="Reconnect dashboard stream">
              <Button variant="plain" aria-label="Reconnect dashboard stream" onClick={reconnect}>
                <RefreshCw aria-hidden="true" />
              </Button>
            </Tooltip>
          </div>
        </MastheadContent>
      </Masthead>

      <SummaryStrip document={document} />

      <Drawer isExpanded={selection !== null} position="end">
        <DrawerContent panelContent={panel}>
          <DrawerContentBody>
            <main className="rr-workspace">
              {(initialError || document?.error) && (
                <Alert
                  isInline
                  variant="danger"
                  title={document?.snapshot ? "Latest probe failed; showing the last successful snapshot" : "Dashboard is unavailable"}
                  className="rr-alert"
                >
                  {document?.error ?? initialError}
                </Alert>
              )}

              <div className="rr-toolbar" aria-label="Dashboard filters">
                <SearchInput
                  aria-label="Search servers, tasks, or run IDs"
                  placeholder="Search servers, tasks, or run IDs"
                  value={query}
                  onChange={(_event, value) => setQuery(value)}
                  onClear={() => setQuery("")}
                />
                <ToggleGroup aria-label="Queue priority filter">
                  {(["all", "urgent", "normal"] as const).map((value) => (
                    <ToggleGroupItem
                      key={value}
                      text={value[0].toUpperCase() + value.slice(1)}
                      buttonId={`priority-${value}`}
                      isSelected={priority === value}
                      onChange={() => setPriority(value)}
                    />
                  ))}
                </ToggleGroup>
              </div>

              <Tabs
                className="rr-mobile-tabs"
                activeKey={mobileView}
                onSelect={(_event, eventKey) => setMobileView(eventKey as MobileView)}
                aria-label="Dashboard tables"
                isFilled
              >
                <Tab eventKey="servers" title={<TabTitleText>Servers</TabTitleText>} />
                <Tab eventKey="queue" title={<TabTitleText>Queue</TabTitleText>} />
              </Tabs>

              <section className={`rr-data-section ${mobileView !== "servers" ? "rr-mobile-hidden" : ""}`} aria-labelledby="servers-title">
                <header className="rr-section-header">
                  <div>
                    <h1 id="servers-title">Server capacity</h1>
                    <p>Controller-wide availability and active assignments</p>
                  </div>
                  <span className="rr-count rr-mono">{serverCount}</span>
                </header>
                {!document ? (
                  <div className="rr-loading" aria-label="Loading server capacity"><Skeleton width="34%" /><Skeleton width="100%" /><Skeleton width="100%" /></div>
                ) : <ServerTable servers={servers} drainedServers={drainedServers} onSelect={setSelection} />}
              </section>

              <section className={`rr-data-section ${mobileView !== "queue" ? "rr-mobile-hidden" : ""}`} aria-labelledby="queue-title">
                <header className="rr-section-header">
                  <div>
                    <h1 id="queue-title">Unassigned queue</h1>
                    <p>Prepared work awaiting controller placement</p>
                  </div>
                  <span className="rr-count rr-mono">{queueCount}</span>
                </header>
                {!document ? (
                  <div className="rr-loading" aria-label="Loading queued work"><Skeleton width="27%" /><Skeleton width="100%" /></div>
                ) : <QueueTable entries={queue} now={now} onSelect={setSelection} />}
              </section>

              <SnapshotHealth document={document} now={now} />
            </main>
          </DrawerContentBody>
        </DrawerContent>
      </Drawer>
    </div>
  );
}
