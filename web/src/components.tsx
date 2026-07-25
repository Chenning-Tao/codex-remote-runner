import {
  Button,
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
  Progress,
  ProgressSize,
} from "@patternfly/react-core";
import {
  Table,
  Tbody,
  Td,
  Th,
  Thead,
  Tr,
} from "@patternfly/react-table";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  CircleOff,
  Clock3,
  Cpu,
  Database,
  FlaskConical,
  Gauge,
  LoaderCircle,
  Server as ServerIcon,
  ShieldAlert,
  WifiOff,
} from "lucide-react";
import type { ReactNode } from "react";
import {
  ageFrom,
  progressLabel,
  progressValue,
  serverLoad,
  serverState,
} from "./format";
import type {
  ActiveRun,
  ConnectionState,
  DashboardDocument,
  QueueEntry,
  Selection,
  ServerSnapshot,
} from "./types";

interface StatusVisual {
  text: string;
  color: "green" | "blue" | "orange" | "red" | "grey" | "teal";
  icon: ReactNode;
}

const serverStatus: Record<string, StatusVisual> = {
  idle: { text: "Ready", color: "green", icon: <CheckCircle2 /> },
  busy: { text: "Running", color: "teal", icon: <Activity /> },
  disabled: { text: "Disabled", color: "grey", icon: <CircleOff /> },
  unreachable: { text: "Unreachable", color: "red", icon: <WifiOff /> },
  misconfigured: { text: "Config error", color: "red", icon: <ShieldAlert /> },
  unknown: { text: "Unknown", color: "grey", icon: <AlertCircle /> },
};

export function ServerStatus({ server, drained = false }: { server: ServerSnapshot; drained?: boolean }) {
  const visual = serverStatus[serverState(server)] ?? serverStatus.unknown;
  return (
    <span className="rr-status-stack">
      <Label isCompact variant="outline" color={visual.color} icon={visual.icon}>
        {visual.text}
      </Label>
      {drained && <Label isCompact color="orange" icon={<CircleOff />}>Drained</Label>}
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
    return <Label isCompact color="orange" icon={<LoaderCircle className="rr-spin" />}>Reconnecting</Label>;
  }
  if (connection === "connecting") {
    return <Label isCompact color="grey" icon={<LoaderCircle className="rr-spin" />}>Connecting</Label>;
  }
  if (probeStatus === "error") {
    return <Label isCompact color="red" icon={<AlertCircle />}>Probe failed</Label>;
  }
  if (probeStatus === "probing") {
    return <Label isCompact color="blue" icon={<LoaderCircle className="rr-spin" />}>Probing</Label>;
  }
  if (probeStatus !== "online") {
    return <Label isCompact color="grey" icon={<LoaderCircle className="rr-spin" />}>Waiting for probe</Label>;
  }
  return <Label isCompact color="green" icon={<CheckCircle2 />}>Controller online</Label>;
}

interface SummaryItemProps {
  icon: ReactNode;
  label: string;
  value: string | number;
  detail: string;
}

function SummaryItem({ icon, label, value, detail }: SummaryItemProps) {
  return (
    <div className="rr-summary-item">
      <span className="rr-summary-icon" aria-hidden="true">{icon}</span>
      <span className="rr-summary-label">{label}</span>
      <strong>{value}</strong>
      <span className="rr-summary-detail">{detail}</span>
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
    <section className="rr-summary" aria-label="Pool summary">
      <SummaryItem icon={<ServerIcon />} label="Capacity" value={`${available} / ${servers.length}`} detail="servers available" />
      <SummaryItem icon={<Activity />} label="Active" value={active} detail="running workloads" />
      <SummaryItem icon={<Clock3 />} label="Queue" value={queued} detail="awaiting placement" />
      <SummaryItem icon={<Database />} label="Output sync" value={pending ? pending : "Idle"} detail={pending ? "items pending" : "nothing pending"} />
    </section>
  );
}

function RunIdentity({ run }: { run: ActiveRun }) {
  return (
    <span className="rr-run-identity">
      <span>{run.label ?? run.run_id ?? "Unknown workload"}</span>
      <span className="rr-subtext rr-mono">{run.run_id ?? "--"}</span>
    </span>
  );
}

function RunProgress({ run }: { run: ActiveRun }) {
  const value = progressValue(run.progress);
  if (value === null) return <span className="rr-muted">No progress reported</span>;
  const label = progressLabel(run);
  return (
    <Progress
      className="rr-progress"
      size={ProgressSize.sm}
      value={value}
      label={value > 0 && value < 0.1 ? `${value.toFixed(2)}%` : `${value.toFixed(0)}%`}
      valueText={label}
      aria-label={`${run.label ?? run.run_id ?? "Workload"} progress`}
      measureLocation="outside"
    />
  );
}

export function ServerTable({
  servers,
  drainedServers,
  onSelect,
}: {
  servers: ServerSnapshot[];
  drainedServers: Set<string>;
  onSelect: (selection: Selection) => void;
}) {
  if (!servers.length) return <div className="rr-empty">No servers match this view.</div>;
  return (
    <div className="rr-table-scroll">
      <Table aria-label="Server capacity" variant="compact" isStriped gridBreakPoint="">
        <Thead>
          <Tr>
            <Th width={15}>Server</Th>
            <Th width={15}>State</Th>
            <Th width={15}>Load 5m / cores</Th>
            <Th width={30}>Active workload</Th>
            <Th width={25}>Progress</Th>
          </Tr>
        </Thead>
        <Tbody>
          {servers.map((server) => {
            const runs = server.active_runs ?? [];
            const drained = drainedServers.has(server.name);
            return (
              <Tr
                key={server.name}
                className="rr-clickable-row"
                tabIndex={0}
                onClick={() => onSelect({ kind: "server", value: server, drained })}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect({ kind: "server", value: server, drained });
                  }
                }}
              >
                <Td dataLabel="Server">
                  <span className="rr-primary-cell"><ServerIcon aria-hidden="true" />{server.name}</span>
                </Td>
                <Td dataLabel="State"><ServerStatus server={server} drained={drained} /></Td>
                <Td dataLabel="Load 5m / cores"><span className="rr-mono">{serverLoad(server)}</span></Td>
                <Td dataLabel="Active workload">
                  {runs.length ? (
                    <div className="rr-run-stack">
                      {runs.map((run) => (
                        <Button
                          key={run.run_id ?? run.label}
                          variant="link"
                          isInline
                          className="rr-run-link"
                          onClick={(event) => {
                            event.stopPropagation();
                            onSelect({ kind: "run", value: run, server, drained });
                          }}
                        >
                          <RunIdentity run={run} />
                        </Button>
                      ))}
                    </div>
                  ) : <span className="rr-muted">Unassigned</span>}
                </Td>
                <Td dataLabel="Progress">
                  <div className="rr-progress-stack">
                    {runs.length ? runs.map((run) => <RunProgress key={run.run_id ?? run.label} run={run} />) : <span className="rr-muted">--</span>}
                  </div>
                </Td>
              </Tr>
            );
          })}
        </Tbody>
      </Table>
    </div>
  );
}

function queuePriority(priority: string | undefined): ReactNode {
  return priority === "urgent"
    ? <Label isCompact color="orange" icon={<AlertCircle />}>Urgent</Label>
    : <Label isCompact color="grey">Normal</Label>;
}

export function QueueTable({
  entries,
  now,
  onSelect,
}: {
  entries: QueueEntry[];
  now: number;
  onSelect: (selection: Selection) => void;
}) {
  if (!entries.length) return <div className="rr-empty">No queued work matches this view.</div>;
  return (
    <div className="rr-table-scroll">
      <Table aria-label="Unassigned queue" variant="compact" isStriped gridBreakPoint="">
        <Thead>
          <Tr>
            <Th width={10}>Priority</Th>
            <Th width={25}>Task</Th>
            <Th width={15}>Class</Th>
            <Th width={25}>Eligible servers</Th>
            <Th width={15}>State</Th>
            <Th width={10}>Waiting</Th>
          </Tr>
        </Thead>
        <Tbody>
          {entries.map((entry) => (
            <Tr
              key={entry.job.run_id ?? entry.job.label}
              className="rr-clickable-row"
              tabIndex={0}
              onClick={() => onSelect({ kind: "queue", value: entry })}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  onSelect({ kind: "queue", value: entry });
                }
              }}
            >
              <Td dataLabel="Priority">{queuePriority(entry.job.queue_priority)}</Td>
              <Td dataLabel="Task"><RunIdentity run={entry.job} /></Td>
              <Td dataLabel="Class">
                <span className="rr-inline-detail"><FlaskConical aria-hidden="true" />{entry.job.workload_class ?? "standard"}</span>
              </Td>
              <Td dataLabel="Eligible servers">
                <span className="rr-inline-detail"><Cpu aria-hidden="true" />{entry.job.eligible_servers?.join(", ") || "None"}</span>
              </Td>
              <Td dataLabel="State">
                <Label isCompact color={entry.state.status === "dispatching" ? "blue" : "grey"} icon={entry.state.status === "dispatching" ? <LoaderCircle className="rr-spin" /> : <Clock3 />}>
                  {entry.state.status ?? "queued"}
                </Label>
              </Td>
              <Td dataLabel="Waiting"><span className="rr-mono">{ageFrom(entry.job.created_at, now)}</span></Td>
            </Tr>
          ))}
        </Tbody>
      </Table>
    </div>
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

export function DetailPanel({ selection, onClose }: { selection: Selection; onClose: () => void }) {
  let title: string;
  let kind: string;
  let body: ReactNode;

  if (selection.kind === "server") {
    const server = selection.value;
    title = server.name;
    kind = "Server";
    body = (
      <>
        <div className="rr-detail-status"><ServerStatus server={server} drained={selection.drained} /></div>
        <DescriptionList isHorizontal>
          <DetailGroup term="Load 1m / 5m / 15m" mono>{[server.load1, server.load5, server.load15].map((value) => typeof value === "number" ? value.toFixed(1) : "--").join(" / ")}</DetailGroup>
          <DetailGroup term="Configured cores" mono>{server.configured_cores ?? "--"}</DetailGroup>
          <DetailGroup term="Remote cores" mono>{server.remote_cores ?? "--"}</DetailGroup>
          <DetailGroup term="Test slots" mono>{server.test_slots ?? 0}</DetailGroup>
          <DetailGroup term="Automatic placement">{server.auto_select === false ? "Excluded" : "Eligible"}</DetailGroup>
          <DetailGroup term="Controller drain">{selection.drained ? "Drained" : "Not drained"}</DetailGroup>
          {(server.configuration_error || server.error) && <DetailGroup term="Error">{server.configuration_error ?? server.error}</DetailGroup>}
        </DescriptionList>
      </>
    );
  } else if (selection.kind === "run") {
    const run = selection.value;
    title = run.label ?? run.run_id ?? "Active workload";
    kind = "Active run";
    body = (
      <DescriptionList isHorizontal>
        <DetailGroup term="Run ID" mono>{run.run_id ?? "--"}</DetailGroup>
        <DetailGroup term="Task ID" mono>{run.task_id ?? "--"}</DetailGroup>
        <DetailGroup term="Server" mono>{selection.server.name}</DetailGroup>
        <DetailGroup term="Controller drain">{selection.drained ? "Drained" : "Not drained"}</DetailGroup>
        <DetailGroup term="Class">{run.workload_class ?? "standard"}</DetailGroup>
        <DetailGroup term="Status">{run.authoritative_status ?? "running"}</DetailGroup>
        <DetailGroup term="Started">{run.started_at ?? "--"}</DetailGroup>
        {run.error && <DetailGroup term="Error">{run.error}</DetailGroup>}
      </DescriptionList>
    );
  } else {
    const entry = selection.value;
    title = entry.job.label ?? entry.job.run_id ?? "Queued run";
    kind = "Queued run";
    body = (
      <DescriptionList isHorizontal>
        <DetailGroup term="Run ID" mono>{entry.job.run_id ?? "--"}</DetailGroup>
        <DetailGroup term="Task ID" mono>{entry.job.task_id ?? "--"}</DetailGroup>
        <DetailGroup term="Priority">{entry.job.queue_priority ?? "normal"}</DetailGroup>
        <DetailGroup term="Class">{entry.job.workload_class ?? "standard"}</DetailGroup>
        <DetailGroup term="Result intent">{entry.job.result_intent ?? "--"}</DetailGroup>
        <DetailGroup term="Eligible servers">{entry.job.eligible_servers?.join(", ") || "None"}</DetailGroup>
        <DetailGroup term="State">{entry.state.status ?? "queued"}</DetailGroup>
        <DetailGroup term="Created">{entry.job.created_at ?? "--"}</DetailGroup>
        {entry.state.error && <DetailGroup term="Error">{entry.state.error}</DetailGroup>}
      </DescriptionList>
    );
  }

  return (
    <DrawerPanelContent widths={{ default: "width_100", lg: "width_50", xl: "width_33" }} focusTrap={{ enabled: true, "aria-labelledby": "rr-detail-title" }}>
      <DrawerHead>
        <div>
          <p className="rr-eyebrow">{kind}</p>
          <h2 id="rr-detail-title">{title}</h2>
        </div>
        <DrawerActions><DrawerCloseButton onClose={onClose} aria-label="Close details" /></DrawerActions>
      </DrawerHead>
      <DrawerPanelBody>{body}</DrawerPanelBody>
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
      <span><Gauge aria-hidden="true" />Snapshot {document?.snapshot ? (stale ? "stale" : "healthy") : "pending"}</span>
      <span className="rr-mono">age {age}</span>
      <span className="rr-mono">interval {document?.probe_interval_seconds ?? "--"}s</span>
    </div>
  );
}
