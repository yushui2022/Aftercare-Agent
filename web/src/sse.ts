import { ApiError, identityHeaders, toError, type Identity } from "./http";
import type { CaseEvent } from "./types";

export interface SseMessage {
  id: string | null;
  event: string;
  data: string;
}

/**
 * Incremental Server-Sent Events decoder.
 *
 * The control plane writes `id:` / `event:` / `data:` lines and a blank line
 * between messages, so a chunk boundary can fall anywhere.  Feeding raw chunks
 * in and getting whole messages out keeps the reconnect/resume logic free of
 * byte-level parsing and makes both halves unit-testable.
 */
export class SseDecoder {
  private buffer = "";

  push(chunk: string): SseMessage[] {
    this.buffer += chunk.replace(/\r\n/g, "\n");
    const messages: SseMessage[] = [];
    let boundary = this.buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const message = decodeBlock(block);
      if (message !== null) {
        messages.push(message);
      }
      boundary = this.buffer.indexOf("\n\n");
    }
    return messages;
  }
}

function decodeBlock(block: string): SseMessage | null {
  let id: string | null = null;
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line === "" || line.startsWith(":")) {
      // Empty lines end a message; `:` lines are keep-alive comments.
      continue;
    }
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) {
      value = value.slice(1);
    }
    if (field === "id") {
      id = value;
    } else if (field === "event") {
      event = value;
    } else if (field === "data") {
      data.push(value);
    }
  }
  if (data.length === 0) {
    return null;
  }
  return { id, event, data: data.join("\n") };
}

/** Map one SSE message to the operator-facing event summary. */
export function parseCaseEvent(message: SseMessage): CaseEvent | null {
  const seq = message.id === null ? Number.NaN : Number(message.id);
  if (!Number.isInteger(seq) || seq < 1) {
    return null;
  }
  let payload: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(message.data);
    if (parsed === null || typeof parsed !== "object") {
      return null;
    }
    payload = parsed as Record<string, unknown>;
  } catch {
    return null;
  }
  const reference = payload["payload"];
  const referenceId =
    reference !== null && typeof reference === "object" && "reference_id" in reference
      ? String((reference as { reference_id: unknown }).reference_id)
      : null;
  return {
    case_seq: seq,
    event_type:
      typeof payload["event_type"] === "string" ? payload["event_type"] : message.event,
    recorded_at: typeof payload["recorded_at"] === "string" ? payload["recorded_at"] : null,
    run_id: typeof payload["run_id"] === "string" ? payload["run_id"] : null,
    reference_id: referenceId,
  };
}

/**
 * Append one event, ignoring a duplicate case sequence and keeping order.
 *
 * The tail can redeliver after a cursor resume, so the timeline must be
 * idempotent per `case_seq`; the cap bounds memory on a long-lived stream.
 */
export function appendEvent(events: CaseEvent[], event: CaseEvent, cap = 500): CaseEvent[] {
  if (events.some((item) => item.case_seq === event.case_seq)) {
    return events;
  }
  const next = [...events, event].sort((left, right) => left.case_seq - right.case_seq);
  return next.length > cap ? next.slice(next.length - cap) : next;
}

/** Deterministic capped exponential backoff, in milliseconds. */
export function nextBackoffMs(attempt: number, baseMs = 1000, maxMs = 15000): number {
  const exponent = Math.max(0, attempt - 1);
  return Math.min(maxMs, baseMs * 2 ** exponent);
}

export type StreamStatus = "connecting" | "live" | "reconnecting" | "stopped";

export interface StreamHandlers {
  onEvents: (events: CaseEvent[]) => void;
  onStatus: (status: StreamStatus, detail?: string) => void;
}

const EVENT_LIMIT = 200;
// The server intentionally bounds a tail to 60s, so a long-lived view is a
// sequence of bounded reads resumed from the last seen cursor.
const WAIT_SECONDS = 60;
const MIN_RECONNECT_MS = 250;
const MAX_CONSECUTIVE_FAILURES = 5;

function streamUrl(caseId: string, after: number): string {
  const params = new URLSearchParams({
    after: String(after),
    limit: String(EVENT_LIMIT),
    follow: "true",
    wait_seconds: String(WAIT_SECONDS),
  });
  return `/api/v1/cases/${encodeURIComponent(caseId)}/events?${params.toString()}`;
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

/**
 * Follow one Case's events, resuming from the last seen cursor after a
 * reconnect.  Returns an unsubscribe function.
 *
 * A 401/403 is treated as fatal: credentials are not going to become valid by
 * polling, so the caller is told to stop instead of hammering the server.
 */
export function subscribeCaseEvents(
  identity: Identity,
  caseId: string,
  handlers: StreamHandlers,
): () => void {
  const controller = new AbortController();
  let cursor = 0;
  let attempt = 0;
  let stopped = false;
  let live = false;

  // A loop rather than recursive awaits: a tail can stay open for hours, and
  // each nested `await connect()` would retain another suspended frame.
  const run = async (): Promise<void> => {
    while (!stopped) {
      handlers.onStatus(attempt === 0 ? "connecting" : "reconnecting");
      let errored = false;
      live = false;
      try {
        const response = await fetch(streamUrl(caseId, cursor), {
          headers: identityHeaders(identity),
          signal: controller.signal,
          cache: "no-store",
        });
        if (!response.ok) {
          throw await toError(response);
        }
        if (response.body === null) {
          throw new Error("流式响应缺少响应体");
        }
        live = true;
        attempt = 0;
        handlers.onStatus("live");
        const reader = response.body.getReader();
        const text = new TextDecoder();
        const decoder = new SseDecoder();
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) {
            break;
          }
          const batch: CaseEvent[] = [];
          for (const message of decoder.push(text.decode(value, { stream: true }))) {
            const event = parseCaseEvent(message);
            if (event !== null) {
              cursor = Math.max(cursor, event.case_seq);
              batch.push(event);
            }
          }
          if (batch.length > 0) {
            handlers.onEvents(batch);
          }
        }
      } catch (cause: unknown) {
        if (stopped || controller.signal.aborted) {
          return;
        }
        if (cause instanceof ApiError && cause.isFatal) {
          stopped = true;
          handlers.onStatus("stopped", cause.message);
          return;
        }
        errored = true;
        handlers.onStatus(
          "reconnecting",
          cause instanceof Error ? cause.message : String(cause),
        );
      }
      if (stopped) {
        return;
      }
      if (errored) {
        attempt += 1;
        if (attempt > MAX_CONSECUTIVE_FAILURES) {
          stopped = true;
          handlers.onStatus("stopped", "连续重连失败，已停止订阅");
          return;
        }
      } else {
        // A bounded tail ended normally; resume immediately from the cursor.
        attempt = 0;
      }
      await delay(errored ? nextBackoffMs(attempt) : MIN_RECONNECT_MS, controller.signal);
    }
  };

  void run();

  return () => {
    if (stopped && !live) {
      return;
    }
    stopped = true;
    controller.abort();
    handlers.onStatus("stopped");
  };
}
