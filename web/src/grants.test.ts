import { describe, expect, it } from "vitest";

import {
  GRANTABLE_PERMISSIONS,
  buildGrantRequest,
  buildRevokeRequest,
  delegablePermissions,
  grantStatus,
  isUsableRevision,
  toLocalInput,
  undelegablePermissions,
} from "./grants";
import type { CaseGrant } from "./types";

function grant(overrides: Partial<CaseGrant> = {}): CaseGrant {
  return {
    case_id: "case-1",
    subject_id: "operator-7",
    permissions: ["case:read"],
    revision: 1,
    granted_by: "grant-admin",
    granted_at: "2026-09-15T01:00:00Z",
    expires_at: null,
    revoked_at: null,
    revoked_by: null,
    updated_at: "2026-09-15T01:00:00Z",
    ...overrides,
  };
}

/** Truncated to the minute, which is all a datetime-local input can express. */
function localMinute(year: number, month: number, day: number, hour: number, minute: number) {
  return new Date(year, month - 1, day, hour, minute, 0, 0);
}

const NOW = localMinute(2026, 9, 15, 12, 0);

describe("grantable permission set", () => {
  it("never offers a tenant-level scope", () => {
    const everything = ["case:create", "grant:read", "grant:admin", ...GRANTABLE_PERMISSIONS];

    expect(delegablePermissions(everything)).toEqual([...GRANTABLE_PERMISSIONS]);
    expect(delegablePermissions(everything)).not.toContain("case:create");
    expect(delegablePermissions(everything)).not.toContain("grant:admin");
  });

  it("keeps only what the acting administrator itself holds", () => {
    const actor = ["case:read", "review:decide", "grant:admin"];

    expect(delegablePermissions(actor)).toEqual(["case:read", "review:decide"]);
    expect(undelegablePermissions(actor)).toEqual([
      "review:read",
      "approval:read",
      "approval:decide",
    ]);
    expect(delegablePermissions([])).toEqual([]);
    expect(undelegablePermissions([])).toEqual([...GRANTABLE_PERMISSIONS]);
  });
});

describe("grant status", () => {
  it("reports an unexpired row as active", () => {
    expect(grantStatus(grant(), NOW)).toBe("active");
    expect(
      grantStatus(grant({ expires_at: new Date(NOW.getTime() + 60_000).toISOString() }), NOW),
    ).toBe("active");
  });

  it("reports an elapsed expiry as expired", () => {
    expect(
      grantStatus(grant({ expires_at: new Date(NOW.getTime() - 1).toISOString() }), NOW),
    ).toBe("expired");
    expect(grantStatus(grant({ expires_at: NOW.toISOString() }), NOW)).toBe("expired");
  });

  it("lets revocation win over any expiry", () => {
    const revoked = grant({
      revoked_at: "2026-09-15T02:00:00Z",
      revoked_by: "grant-admin",
      expires_at: new Date(NOW.getTime() - 1).toISOString(),
    });

    expect(grantStatus(revoked, NOW)).toBe("revoked");
  });
});

describe("grant request builder", () => {
  it("builds a first grant without a revision and with sorted permissions", () => {
    const result = buildGrantRequest(
      {
        subjectId: "  operator-7  ",
        permissions: ["review:decide", "case:read", "case:read"],
        expiresAt: "",
        expectedRevision: null,
      },
      NOW,
    );

    expect(result).toEqual({
      ok: true,
      body: {
        subject_id: "operator-7",
        permissions: ["case:read", "review:decide"],
        expires_at: null,
        expected_revision: null,
      },
    });
  });

  it("converts a local expiry into the instant the server requires", () => {
    const expiry = localMinute(2026, 9, 16, 10, 30);
    const result = buildGrantRequest(
      {
        subjectId: "operator-7",
        permissions: ["case:read"],
        expiresAt: "2026-09-16T10:30",
        expectedRevision: 1,
      },
      NOW,
    );

    expect(result).toEqual({
      ok: true,
      body: {
        subject_id: "operator-7",
        permissions: ["case:read"],
        expires_at: expiry.toISOString(),
        expected_revision: 1,
      },
    });
  });

  it("rejects an expiry that is not in the future", () => {
    const result = buildGrantRequest(
      {
        subjectId: "operator-7",
        permissions: ["case:read"],
        expiresAt: "2026-09-15T11:59",
        expectedRevision: null,
      },
      NOW,
    );

    expect(result).toEqual({ ok: false, message: "有效期必须晚于当前时间" });
  });

  it("rejects an unparsable expiry rather than guessing", () => {
    const result = buildGrantRequest(
      {
        subjectId: "operator-7",
        permissions: ["case:read"],
        expiresAt: "not-a-time",
        expectedRevision: null,
      },
      NOW,
    );

    expect(result.ok).toBe(false);
  });

  it("rejects a subject that is not a bounded identifier", () => {
    const draft = {
      permissions: ["case:read"],
      expiresAt: "",
      expectedRevision: null,
    };

    for (const subjectId of ["", "   ", "-leading", "has space", "a".repeat(161)]) {
      expect(buildGrantRequest({ ...draft, subjectId }, NOW).ok).toBe(false);
    }
    for (const subjectId of ["operator-7", "svc:aftercare/ops_1"]) {
      expect(buildGrantRequest({ ...draft, subjectId }, NOW).ok).toBe(true);
    }
  });

  it("refuses an empty or out-of-set permission list", () => {
    const base = { subjectId: "operator-7", expiresAt: "", expectedRevision: null };

    expect(buildGrantRequest({ ...base, permissions: [] }, NOW)).toEqual({
      ok: false,
      message: "至少选择一个要授予的权限",
    });
    for (const permission of ["case:create", "grant:admin", "approval:approve", "*"]) {
      const result = buildGrantRequest({ ...base, permissions: [permission] }, NOW);
      expect(result.ok).toBe(false);
    }
  });

  it("requires a positive revision when one is supplied", () => {
    const base = {
      subjectId: "operator-7",
      permissions: ["case:read"],
      expiresAt: "",
    };

    expect(buildGrantRequest({ ...base, expectedRevision: 0 }, NOW).ok).toBe(false);
    expect(buildGrantRequest({ ...base, expectedRevision: 1 }, NOW).ok).toBe(true);
  });
});

describe("revision helpers", () => {
  it("accepts only positive integers", () => {
    expect(isUsableRevision(1)).toBe(true);
    expect(isUsableRevision(7)).toBe(true);
    expect(isUsableRevision(null)).toBe(false);
    expect(isUsableRevision(0)).toBe(false);
    expect(isUsableRevision(-1)).toBe(false);
    expect(isUsableRevision(1.5)).toBe(false);
  });

  it("refuses to revoke without a usable revision", () => {
    expect(buildRevokeRequest(3)).toEqual({ ok: true, body: { expected_revision: 3 } });
    expect(buildRevokeRequest(null).ok).toBe(false);
  });
});

describe("local input conversion", () => {
  it("keeps the instant and truncates to whole minutes", () => {
    const iso = localMinute(2026, 9, 16, 8, 5).toISOString();

    expect(new Date(toLocalInput(iso)).getTime()).toBe(new Date(iso).getTime());
    expect(toLocalInput(iso)).toBe("2026-09-16T08:05");
  });

  it("renders an absent or unparsable value as empty", () => {
    expect(toLocalInput(null)).toBe("");
    expect(toLocalInput("not-a-time")).toBe("");
  });
});
