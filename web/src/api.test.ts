import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

const identity = { tenantId: "tenant-a", subjectId: "operator-1" };

afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("operator API client", () => {
  it("sends both parts of a keyset cursor and preserves identity headers", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ cases: [], next_created_at: null, next_case_id: null }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.listCases(identity, {
      status: "OPEN",
      limit: 20,
      afterCreatedAt: "2026-09-14T01:02:03Z",
      afterCaseId: "case-19",
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "/api/v1/cases?status=OPEN&limit=20&after_created_at=2026-09-14T01%3A02%3A03Z&after_case_id=case-19",
    );
    expect(new Headers(init.headers).get("X-Synthetic-Tenant")).toBe("tenant-a");
    expect(new Headers(init.headers).get("X-Synthetic-Subject")).toBe("operator-1");
  });

  it("uses the caller supplied idempotency key for decisions", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ review_id: "review-1" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.decideReview(identity, "case-1", "review-1", "CONTINUE", "ok", "retry-key-1");

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("retry-key-1");
    expect(JSON.parse(String(init.body))).toEqual({
      decision: "CONTINUE",
      decision_reason: "ok",
    });
  });
});
