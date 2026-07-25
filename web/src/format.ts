import type { ActiveRun, RunProgress, ServerSnapshot } from "./types";

export function duration(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  if (safe < 60) return `${safe}s`;
  const minutes = Math.floor(safe / 60);
  const remainder = safe % 60;
  if (minutes < 60) return `${minutes}m ${remainder.toString().padStart(2, "0")}s`;
  const hours = Math.floor(minutes / 60);
  const minuteRemainder = minutes % 60;
  if (hours < 24) return `${hours}h ${minuteRemainder.toString().padStart(2, "0")}m`;
  return `${Math.floor(hours / 24)}d ${String(hours % 24).padStart(2, "0")}h`;
}

export function ageFrom(value: string | null | undefined, now: number): string {
  if (!value) return "--";
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) return "--";
  return duration((now - timestamp) / 1000);
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
  if (value === null) return "Awaiting progress";
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
  const load = typeof server.load5 === "number" ? server.load5.toFixed(1) : "--";
  const cores = typeof server.configured_cores === "number" ? server.configured_cores : "--";
  return `${load} / ${cores}`;
}

export function textMatches(parts: Array<string | undefined>, query: string): boolean {
  if (!query) return true;
  return parts.join(" ").toLocaleLowerCase().includes(query.toLocaleLowerCase());
}
