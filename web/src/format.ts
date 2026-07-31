import type { ActiveRun, RunProgress, ServerSnapshot } from "./types";

const BYTES_PER_GIB = 1024 ** 3;

const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "medium",
});

export function duration(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  if (safe < 60) return `${safe}秒`;
  const minutes = Math.floor(safe / 60);
  const remainder = safe % 60;
  if (minutes < 60) return `${minutes}分 ${remainder.toString().padStart(2, "0")}秒`;
  const hours = Math.floor(minutes / 60);
  const minuteRemainder = minutes % 60;
  if (hours < 24) return `${hours}小时 ${minuteRemainder.toString().padStart(2, "0")}分`;
  return `${Math.floor(hours / 24)}天 ${String(hours % 24).padStart(2, "0")}小时`;
}

export function ageFrom(value: string | null | undefined, now: number): string {
  if (!value) return "--";
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "--";
  return duration((now - timestamp) / 1000);
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return "--";
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "--";
  return dateTimeFormatter.format(timestamp);
}

export function progressValue(progress: RunProgress | undefined): number | null {
  if (!progress) return null;
  if (
    typeof progress.current === "number" &&
    typeof progress.total === "number" &&
    Number.isFinite(progress.current) &&
    Number.isFinite(progress.total) &&
    progress.total > 0
  ) {
    return Math.min(100, Math.max(0, (progress.current / progress.total) * 100));
  }
  if (typeof progress.percent === "number" && Number.isFinite(progress.percent)) {
    return Math.min(100, Math.max(0, progress.percent));
  }
  return null;
}

export function progressLabel(run: ActiveRun): string {
  const progress = run.progress;
  const value = progressValue(progress);
  const stage = progress?.scope && progress.stage
    ? `${progress.scope}:${progress.stage}`
    : progress?.stage;
  if (stage) return stage;
  if (value === null) return "等待进度";
  return value > 0 && value < 0.1 ? `${value.toFixed(2)}%` : `${value.toFixed(1)}%`;
}

export function serverState(server: ServerSnapshot): string {
  if (server.configuration_error || server.state === "configuration_error") {
    return "misconfigured";
  }
  if (server.enabled === false) return "disabled";
  return server.state ?? "unknown";
}

export function serverLoad(server: ServerSnapshot): string {
  const load = typeof server.load1 === "number" ? server.load1.toFixed(1) : "--";
  const cores = typeof server.configured_cores === "number" ? server.configured_cores : "--";
  return `${load} / ${cores}`;
}

function memoryBytes(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function memoryAmount(bytes: number): string {
  const gib = bytes / BYTES_PER_GIB;
  if (gib >= 100) return gib.toFixed(0);
  if (gib >= 10) return gib.toFixed(1);
  return gib.toFixed(2);
}

export function memoryPercent(server: ServerSnapshot): number | null {
  const reported = server.memory_used_percent;
  if (typeof reported === "number" && Number.isFinite(reported)) {
    return Math.min(100, Math.max(0, reported));
  }
  const total = memoryBytes(server.memory_total_bytes);
  const used = memoryBytes(server.memory_used_bytes);
  if (total === null || total <= 0 || used === null) return null;
  return Math.min(100, Math.max(0, (used / total) * 100));
}

export function serverMemory(server: ServerSnapshot): string {
  const total = memoryBytes(server.memory_total_bytes);
  if (total === null || total <= 0) return "--";
  const available = memoryBytes(server.memory_available_bytes);
  const reportedUsed = memoryBytes(server.memory_used_bytes);
  const used = reportedUsed ?? (
    available === null ? null : Math.max(0, total - Math.min(total, available))
  );
  return `${used === null ? "--" : memoryAmount(used)} / ${memoryAmount(total)} GiB`;
}

export function textMatches(parts: Array<string | undefined>, query: string): boolean {
  if (!query) return true;
  return parts.join(" ").toLocaleLowerCase().includes(query.toLocaleLowerCase());
}
