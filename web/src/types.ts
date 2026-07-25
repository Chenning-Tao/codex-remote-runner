export type ConnectionState = "connecting" | "live" | "reconnecting";
export type ProbeStatus = "connecting" | "probing" | "online" | "error";

export interface RunProgress {
  current?: number;
  total?: number;
  percent?: number;
  eta_seconds?: number;
  scope?: string;
  stage?: string;
}

export interface ActiveRun {
  run_id?: string;
  label?: string;
  task_id?: string;
  workload_class?: string;
  authoritative_status?: string;
  progress?: RunProgress;
  error?: string;
  started_at?: string;
}

export interface ServerSnapshot {
  name: string;
  state?: string;
  enabled?: boolean;
  auto_select?: boolean;
  configured_cores?: number | null;
  remote_cores?: number | null;
  test_slots?: number;
  load1?: number;
  load5?: number;
  load15?: number;
  standard_runs?: number;
  test_runs?: number;
  active_runs?: ActiveRun[];
  error?: string;
  configuration_error?: string | null;
}

export interface QueueJob {
  run_id?: string;
  label?: string;
  task_id?: string;
  result_intent?: string;
  workload_class?: string;
  queue_priority?: string;
  server_scope?: string;
  created_at?: string;
  eligible_servers?: string[];
}

export interface QueueState {
  status?: string;
  revision?: number;
  updated_at?: string;
  error?: string | null;
}

export interface QueueEntry {
  job: QueueJob;
  state: QueueState;
}

export interface StatusSummary {
  queue?: {
    total?: number;
    active?: number;
    matched?: number;
    returned?: number;
    omitted?: number;
    by_status?: Record<string, number>;
  };
  runs?: {
    total?: number;
    active?: number;
    matched?: number;
    returned?: number;
    omitted?: number;
    by_authoritative_status?: Record<string, number>;
  };
}

export interface DashboardSnapshot {
  servers?: ServerSnapshot[];
  queue?: QueueEntry[];
  runs?: ActiveRun[];
  summary?: StatusSummary;
  output_sync?: Record<string, unknown>;
  server_drains?: {
    scope?: string;
    servers?: Record<string, unknown> | string[];
  };
  collected_at?: string;
}

export interface DashboardDocument {
  schema_version: number;
  project_id: string;
  sequence: number;
  status: ProbeStatus;
  snapshot: DashboardSnapshot | null;
  error: string | null;
  refreshed_at: string | null;
  next_probe_at: string | null;
  probe_interval_seconds: number;
}

export type Selection =
  | { kind: "server"; value: ServerSnapshot; drained: boolean }
  | { kind: "run"; value: ActiveRun; server: ServerSnapshot; drained: boolean }
  | { kind: "queue"; value: QueueEntry };
