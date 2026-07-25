import { useCallback, useEffect, useState } from "react";
import type { ConnectionState, DashboardDocument } from "./types";

interface DashboardResult {
  document: DashboardDocument | null;
  connection: ConnectionState;
  initialError: string | null;
  reconnect: () => void;
}

function parseDocument(value: string): DashboardDocument {
  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== "object" || !("schema_version" in parsed)) {
    throw new Error("Dashboard returned an invalid snapshot");
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

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const events = new EventSource("/api/events");

    fetch("/api/snapshot", { signal: controller.signal, cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`Dashboard request failed (${response.status})`);
        return response.text();
      })
      .then((body) => {
        if (!cancelled) setDocument(parseDocument(body));
      })
      .catch((error: unknown) => {
        if (!cancelled && !(error instanceof DOMException && error.name === "AbortError")) {
          setInitialError(error instanceof Error ? error.message : "Dashboard request failed");
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
        setInitialError(error instanceof Error ? error.message : "Invalid dashboard event");
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

  return { document, connection, initialError, reconnect };
}
