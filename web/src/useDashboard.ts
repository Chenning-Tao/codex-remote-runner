import { useCallback, useEffect, useState } from "react";
import type {
  BatchQueueUpdateItem,
  BatchQueueUpdateResult,
  CapacityUpdateChanges,
  ConnectionState,
  DashboardDocument,
  QueueUpdateChanges,
} from "./types";

interface DashboardResult {
  document: DashboardDocument | null;
  connection: ConnectionState;
  initialError: string | null;
  reconnect: () => void;
  stopRun: (runId: string) => Promise<void>;
  updateQueue: (
    runId: string,
    expectedRevision: number,
    changes: QueueUpdateChanges,
  ) => Promise<void>;
  updateQueueBatch: (
    updates: BatchQueueUpdateItem[],
    eligibleServers: string[],
  ) => Promise<BatchQueueUpdateResult>;
  updateCapacity: (
    server: string,
    expectedRevision: number,
    changes: CapacityUpdateChanges,
  ) => Promise<void>;
  updateServerDrain: (server: string, drained: boolean) => Promise<void>;
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

export class QueueUpdateError extends Error {
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
    this.name = "QueueUpdateError";
  }
}

export class ServerDrainError extends Error {
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
    this.name = "ServerDrainError";
  }
}

const stopErrorMessages: Record<string, string> = {
  run_not_found: "这个任务已经结束或不存在，列表已刷新。",
  run_dispatching: "任务正在分配服务器，请稍后重试。",
  stop_failed: "无法停止这个任务，请稍后重试。",
};

const queueErrorMessages: Record<string, string> = {
  queue_not_found: "这个排队任务已经不存在，列表已刷新。",
  queue_not_editable: "任务已经开始分配服务器，无法再修改队列设置。",
  queue_conflict: "任务刚刚发生了变化，请根据刷新后的队列重试。",
  invalid_queue_update: "队列设置无效，请检查优先级和服务器选择。",
  queue_preparation_failed: "服务器准备失败，原调度设置保持不变。",
};

const capacityErrorMessages: Record<string, string> = {
  capacity_not_found: "这台服务器已经不在当前项目中，列表已刷新。",
  capacity_conflict: "服务器容量刚刚发生了变化，请根据刷新后的数值重试。",
  invalid_capacity_update: "容量设置无效，请输入 0 到 1024 之间的整数。",
};

const serverDrainErrorMessages: Record<string, string> = {
  server_not_found: "这台服务器已经不在当前项目中，列表已刷新。",
  server_drain_failed: "无法修改服务器调度状态，请稍后重试。",
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

  const updateQueue = useCallback(async (
    runId: string,
    expectedRevision: number,
    changes: QueueUpdateChanges,
  ) => {
    const response = await fetch(`/api/queue/${encodeURIComponent(runId)}`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-Remote-Runner-Action": "update-queue",
      },
      body: JSON.stringify({
        run_id: runId,
        expected_revision: expectedRevision,
        ...changes,
      }),
    });
    if (response.ok) return;
    let code = "invalid_queue_update";
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
    throw new QueueUpdateError(
      detail ?? queueErrorMessages[code] ?? `队列修改失败（${response.status}）`,
      code,
    );
  }, []);

  const updateQueueBatch = useCallback(async (
    updates: BatchQueueUpdateItem[],
    eligibleServers: string[],
  ): Promise<BatchQueueUpdateResult> => {
    const response = await fetch("/api/queue-batch", {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-Remote-Runner-Action": "update-queue-batch",
      },
      body: JSON.stringify({ updates, eligible_servers: eligibleServers }),
    });
    let payload: unknown = null;
    try {
      payload = await response.json();
    } catch {
      // The status-based error below remains useful for a non-JSON response.
    }
    if (
      response.ok
      && payload
      && typeof payload === "object"
      && "status" in payload
      && "succeeded" in payload
      && Array.isArray(payload.succeeded)
      && "failed" in payload
      && Array.isArray(payload.failed)
    ) {
      return payload as BatchQueueUpdateResult;
    }
    let code = "invalid_queue_update";
    let detail: string | null = null;
    if (payload && typeof payload === "object" && "error" in payload && typeof payload.error === "string") {
      code = payload.error;
      if ("detail" in payload && typeof payload.detail === "string") detail = payload.detail;
    }
    throw new QueueUpdateError(
      detail ?? queueErrorMessages[code] ?? `批量修改失败（${response.status}）`,
      code,
    );
  }, []);

  const updateCapacity = useCallback(async (
    server: string,
    expectedRevision: number,
    changes: CapacityUpdateChanges,
  ) => {
    const response = await fetch(`/api/servers/${encodeURIComponent(server)}/capacity`, {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
        "X-Remote-Runner-Action": "update-capacity",
      },
      body: JSON.stringify({ server, expected_revision: expectedRevision, ...changes }),
    });
    if (response.ok) return;
    let code = "invalid_capacity_update";
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
        if ("detail" in payload && typeof payload.detail === "string") detail = payload.detail;
      }
    } catch {
      // Keep the status-based fallback when the server response is not JSON.
    }
    throw new QueueUpdateError(
      detail ?? capacityErrorMessages[code] ?? `容量修改失败（${response.status}）`,
      code,
    );
  }, []);

  const updateServerDrain = useCallback(async (server: string, drained: boolean) => {
    const operation = drained ? "drain" : "resume";
    const response = await fetch(`/api/servers/${encodeURIComponent(server)}/${operation}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Remote-Runner-Action": drained ? "drain-server" : "resume-server",
      },
      body: JSON.stringify({ server, confirm: true }),
    });
    if (response.ok) return;
    let code = "server_drain_failed";
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
        if ("detail" in payload && typeof payload.detail === "string") detail = payload.detail;
      }
    } catch {
      // Keep the status-based fallback when the server response is not JSON.
    }
    throw new ServerDrainError(
      detail
        ?? serverDrainErrorMessages[code]
        ?? `调度状态修改失败（${response.status}）`,
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

  return {
    document,
    connection,
    initialError,
    reconnect,
    stopRun,
    updateQueue,
    updateQueueBatch,
    updateCapacity,
    updateServerDrain,
  };
}
