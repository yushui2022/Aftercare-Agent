import { describe, expect, it } from "vitest";

import { SseDecoder, appendEvent, nextBackoffMs, parseCaseEvent } from "./sse";
import type { CaseEvent } from "./types";

function event(seq: number, type = "review.requested"): CaseEvent {
  return {
    case_seq: seq,
    event_type: type,
    recorded_at: "2026-09-13T10:00:00Z",
    run_id: "run-1",
    reference_id: `event-payload:evt-${String(seq)}`,
  };
}

function block(seq: number, type = "review.requested"): string {
  // Mirrors aftercare_agent/api/app.py's stream() formatting exactly.
  const payload = JSON.stringify({
    event_type: type,
    recorded_at: "2026-09-13T10:00:00Z",
    run_id: "run-1",
    payload: { reference_id: `event-payload:evt-${String(seq)}` },
  });
  return `id: ${String(seq)}\nevent: ${type}\ndata: ${payload}\n\n`;
}

describe("SseDecoder", () => {
  it("decodes several messages delivered in one chunk", () => {
    const decoder = new SseDecoder();
    const messages = decoder.push(block(1) + block(2));
    expect(messages.map((message) => message.id)).toEqual(["1", "2"]);
    expect(messages.map((message) => message.event)).toEqual([
      "review.requested",
      "review.requested",
    ]);
  });

  it("reassembles a message split across chunk boundaries", () => {
    const decoder = new SseDecoder();
    const whole = block(7);
    const split = Math.floor(whole.length / 2);
    expect(decoder.push(whole.slice(0, split))).toEqual([]);
    const messages = decoder.push(whole.slice(split));
    expect(messages).toHaveLength(1);
    expect(messages[0]?.id).toBe("7");
  });

  it("ignores keep-alive comments and blank padding", () => {
    const decoder = new SseDecoder();
    const messages = decoder.push(": keep-alive\n\n" + block(3));
    expect(messages.map((message) => message.id)).toEqual(["3"]);
  });

  it("normalizes CRLF line endings", () => {
    const decoder = new SseDecoder();
    const messages = decoder.push(block(4).replace(/\n/g, "\r\n"));
    expect(messages.map((message) => message.id)).toEqual(["4"]);
  });

  it("decodes a message that carries no id", () => {
    const decoder = new SseDecoder();
    const messages = decoder.push('event: message\ndata: {"x":1}\n\n');
    expect(messages).toHaveLength(1);
    expect(messages[0]?.id).toBeNull();
  });
});

describe("parseCaseEvent", () => {
  it("maps a well-formed message to the operator projection", () => {
    const decoder = new SseDecoder();
    const [message] = decoder.push(block(9));
    expect(message).toBeDefined();
    const parsed = message === undefined ? null : parseCaseEvent(message);
    expect(parsed).toEqual({
      case_seq: 9,
      event_type: "review.requested",
      recorded_at: "2026-09-13T10:00:00Z",
      run_id: "run-1",
      reference_id: "event-payload:evt-9",
    });
  });

  it("rejects a non-numeric cursor instead of inventing a sequence", () => {
    expect(parseCaseEvent({ id: "abc", event: "x", data: "{}" })).toBeNull();
    expect(parseCaseEvent({ id: null, event: "x", data: "{}" })).toBeNull();
    expect(parseCaseEvent({ id: "0", event: "x", data: "{}" })).toBeNull();
  });

  it("rejects malformed JSON rather than showing a broken row", () => {
    expect(parseCaseEvent({ id: "5", event: "x", data: "{not json" })).toBeNull();
  });
});

describe("appendEvent", () => {
  it("keeps the timeline ordered and unique by case_seq", () => {
    const merged = appendEvent(appendEvent([event(1)], event(3)), event(1));
    expect(merged.map((item) => item.case_seq)).toEqual([1, 3]);
  });

  it("inserts an out-of-order redelivery in sequence position", () => {
    const merged = appendEvent([event(1), event(3)], event(2));
    expect(merged.map((item) => item.case_seq)).toEqual([1, 2, 3]);
  });

  it("returns the same reference when the event is already present", () => {
    const current = [event(1)];
    expect(appendEvent(current, event(1))).toBe(current);
  });

  it("bounds memory by dropping the oldest events past the cap", () => {
    let current: CaseEvent[] = [];
    for (let seq = 1; seq <= 6; seq += 1) {
      current = appendEvent(current, event(seq), 3);
    }
    expect(current.map((item) => item.case_seq)).toEqual([4, 5, 6]);
  });
});

describe("nextBackoffMs", () => {
  it("grows exponentially and then stays capped", () => {
    expect(nextBackoffMs(1)).toBe(1000);
    expect(nextBackoffMs(2)).toBe(2000);
    expect(nextBackoffMs(3)).toBe(4000);
    expect(nextBackoffMs(10)).toBe(15000);
    expect(nextBackoffMs(0)).toBe(1000);
  });
});
