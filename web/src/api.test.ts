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

  it("reads one case's access rows through the bounded list route", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ grants: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await api.listCaseGrants(identity, "case/1", 25);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/cases/case%2F1/grants?limit=25");
    expect(new Headers(init.headers).get("X-Synthetic-Subject")).toBe("operator-1");
  });

  it("sends the revision the operator read instead of an idempotency key", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ revision: 2 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.grantCaseAccess(identity, "case-1", {
      subject_id: "operator-7",
      permissions: ["case:read"],
      expires_at: "2026-09-16T00:00:00.000Z",
      expected_revision: 2,
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/cases/case-1/grants");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    // Optimistic concurrency replaces the idempotency key on this route.
    expect(new Headers(init.headers).get("Idempotency-Key")).toBeNull();
    expect(JSON.parse(String(init.body))).toEqual({
      subject_id: "operator-7",
      permissions: ["case:read"],
      expires_at: "2026-09-16T00:00:00.000Z",
      expected_revision: 2,
    });
  });

  it("revokes through the subject path segment", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ revision: 4 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.revokeCaseAccess(identity, "case-1", "operator-7", { expected_revision: 3 });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/cases/case-1/grants/operator-7/revoke");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ expected_revision: 3 });
  });
});
