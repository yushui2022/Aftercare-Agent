import { describe, expect, it } from "vitest";

import {
  administrationRows,
  appendPage,
  canReadContent,
  queueRows,
} from "./cases";
import type { CaseSummary } from "./types";

function summary(caseId: string, permissions: string[]): CaseSummary {
  return {
    case_id: caseId,
    order_id: `order-${caseId}`,
    status: "OPEN",
    version: 1,
    created_at: "2026-09-15T00:00:00Z",
    permissions,
  };
}

describe("work queue rows", () => {
  it("marks a row without a Case grant as administrable only", () => {
    const readable = summary("case-1", ["case:read", "review:decide"]);
    const administrable = summary("case-2", ["grant:read", "grant:admin"]);

    expect(canReadContent(readable)).toBe(true);
    expect(canReadContent(administrable)).toBe(false);
    expect(queueRows([readable, administrable])).toEqual([
      { summary: readable, administrationOnly: false },
      { summary: administrable, administrationOnly: true },
    ]);
  });

  it("keeps a Case the queue already shows out of the administration block", () => {
    const shared = summary("case-1", ["case:read", "grant:read"]);
    const hidden = summary("case-2", ["grant:read"]);

    expect(administrationRows([shared], [shared, hidden])).toEqual([
      { summary: hidden, administrationOnly: true },
    ]);
  });

  it("appends only the rows a page has not loaded yet", () => {
    const first = summary("case-1", ["case:read"]);
    const second = summary("case-2", ["case:read"]);
    const third = summary("case-3", ["case:read"]);

    expect(appendPage([first, second], [second, third])).toEqual([first, second, third]);
  });

  it("never lets an inventory row claim Case content", () => {
    // The server projects inventory rows with the caller's own `grant:*`
    // scopes only, so `administrationOnly` must stay independent of what the
    // row happens to list: only `case:read` opens content.
    const row = administrationRows([], [summary("case-9", ["grant:admin"])])[0];

    expect(row?.administrationOnly).toBe(true);
  });
});
