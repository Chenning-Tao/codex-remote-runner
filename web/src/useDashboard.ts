import { useCallback, useEffect, useState } from "react";
import type { ConnectionState, DashboardDocument } from "./types";

interface DashboardResult {
  document: DashboardDocument | null;
  connection: ConnectionState;
  initialError: string | null;
  reconnect: () => void;
  stopRun: (runId: string) => Promise<void>;
}

export class StopRunError extends Error {
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
    this.name = "StopRunError";
  }
}

const stopErrorMessages: Record<string, string> = {
  run_not_found: "这个任务已经结束或不存在，列表已刷新。",
  run_dispatching: "任务正在分配服务器，请稍后重试。",
  stop_failed: "无法停止这个任务，请稍后重试。",
};

function parseDocument(value: string): DashboardDocument {
  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || !("schema_version" in parsed)) {
    throw new Error("仪表盘返回了无效快照");
  }
  return parsed as DashboardDocument;
}

export function useDashboard(): DashboardResult {
  const [document, setDocument] = useState<DashboardDocument | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [initialError, setInitialError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);

  const reconnect = useCallback(() => {
    setConnection("connecting");
    setInitialError(null);
    setGeneration((value) => value + 1);
  }, []);

  const stopRun = useCallback(async (runId: string) => {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/stop`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Remote-Runner-Action": "stop",
      },
      body: JSON.stringify({ run_id: runId, confirm: true }),
    });
    if (response.ok) return;
    let code = "stop_failed";
    let detail: string | null = null;
    try {
      const payload: unknown = await response.json();
      if (
        payload
        && typeof payload === "object"
        && "error" in payload
        && typeof payload.error === "string"
      ) {
        code = payload.error;
        if ("detail" in payload && typeof payload.detail === "string") {
          detail = payload.detail;
        }
      }
    } catch {
      // Keep the status-based fallback when the server response is not JSON.
    }
    throw new StopRunError(
      stopErrorMessages[code] ?? detail ?? `停止请求失败（${response.status}）`,
      code,
    );
  }, []);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const events = new EventSource("/api/events");

    fetch("/api/snapshot", { signal: controller.signal, cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`仪表盘请求失败（${response.status}）`);
        return response.text();
      })
      .then((body) => {
        if (!cancelled) setDocument(parseDocument(body));
      })
      .catch((error: unknown) => {
        if (!cancelled && !(error instanceof DOMException && error.name === "AbortError")) {
          setInitialError(error instanceof Error ? error.message : "仪表盘请求失败");
        }
      });

    events.onopen = () => {
      if (!cancelled) setConnection("live");
    };
    events.addEventListener("snapshot", (event) => {
      if (cancelled || !(event instanceof MessageEvent)) return;
      try {
        setDocument(parseDocument(event.data));
        setInitialError(null);
      } catch (error: unknown) {
        setInitialError(error instanceof Error ? error.message : "无效的仪表盘事件");
      }
    });
    events.onerror = () => {
      if (!cancelled) setConnection("reconnecting");
    };

    return () => {
      cancelled = true;
      controller.abort();
      events.close();
    };
  }, [generation]);

  return { document, connection, initialError, reconnect, stopRun };
}
