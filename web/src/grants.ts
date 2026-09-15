import type { CaseGrant } from "./types";

/**
 * Mirrors GRANTABLE_CASE_PERMISSIONS in aftercare_agent/auth/grants.py.
 *
 * The server stays the authority: this copy only lets the form offer exactly
 * what the server accepts.  Drift shows up as a `400 invalid_input` rather
 * than as a permission the UI silently cannot hand out.  Tenant-level scopes
 * such as `case:create` and `grant:admin` are deliberately absent on both
 * sides - one Case row must not widen into tenant authority.
 */
export const GRANTABLE_PERMISSIONS = [
  "case:read",
  "review:read",
  "review:decide",
  "approval:read",
  "approval:decide",
] as const;

export type GrantablePermission = (typeof GRANTABLE_PERMISSIONS)[number];

export const GRANT_READ_PERMISSION = "grant:read";
export const GRANT_ADMIN_PERMISSION = "grant:admin";

/** Identifier bound from aftercare_agent/domain/common.py. */
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:/-]*$/;
const IDENTIFIER_MAX = 160;

function isGrantable(value: string): value is GrantablePermission {
  return (GRANTABLE_PERMISSIONS as readonly string[]).includes(value);
}

/** Permissions this actor may hand out: the closed set minus what it lacks. */
export function delegablePermissions(actorPermissions: readonly string[]): GrantablePermission[] {
  return GRANTABLE_PERMISSIONS.filter((permission) => actorPermissions.includes(permission));
}

/** Grantable permissions the actor does not hold, so the form can say why. */
export function undelegablePermissions(actorPermissions: readonly string[]): GrantablePermission[] {
  return GRANTABLE_PERMISSIONS.filter((permission) => !actorPermissions.includes(permission));
}

export type GrantStatus = "active" | "expired" | "revoked";

/**
 * Effective status of one grant row.
 *
 * A row keeps its history after revocation, and an expiry is fixed at write
 * time, so both are derived here rather than read from a status column.
 */
export function grantStatus(grant: CaseGrant, now: Date): GrantStatus {
  if (grant.revoked_at !== null) {
    return "revoked";
  }
  if (grant.expires_at !== null) {
    const expiry = new Date(grant.expires_at);
    if (!Number.isNaN(expiry.getTime()) && expiry.getTime() <= now.getTime()) {
      return "expired";
    }
  }
  return "active";
}

export interface GrantDraft {
  subjectId: string;
  permissions: readonly string[];
  /** Raw value of an `<input type="datetime-local">`: local time, no zone. */
  expiresAt: string;
  /** Current row revision when replacing; null for the first grant. */
  expectedRevision: number | null;
}

export interface GrantBody {
  subject_id: string;
  permissions: string[];
  expires_at: string | null;
  expected_revision: number | null;
}

export interface RevokeBody {
  expected_revision: number;
}

export type DraftResult<T> = { ok: true; body: T } | { ok: false; message: string };

export function isUsableRevision(value: number | null): value is number {
  return value !== null && Number.isInteger(value) && value >= 1;
}

/**
 * Local `<input type="datetime-local">` value for a server instant.
 *
 * The server always sends a timezone-aware instant; the input control only
 * understands local wall-clock time, so the conversion happens exactly here.
 */
export function toLocalInput(value: string | null): string {
  if (value === null) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const pad = (part: number): string => String(part).padStart(2, "0");
  return (
    `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/**
 * Build the request body for a grant or replacement, or explain what to fix.
 *
 * The rules match the server so the operator sees the problem before a round
 * trip: a bounded identifier, at least one permission drawn from the closed
 * set, and an expiry that is a real instant in the future.  The server checks
 * the expiry against its own database clock, so this can only ever reject
 * earlier, never approve later.
 */
export function buildGrantRequest(
  draft: GrantDraft,
  now: Date = new Date(),
): DraftResult<GrantBody> {
  const subjectId = draft.subjectId.trim();
  if (subjectId === "" || subjectId.length > IDENTIFIER_MAX || !IDENTIFIER.test(subjectId)) {
    return { ok: false, message: "被授权主体只能是字母、数字开头的标识（可用 . _ : / -）" };
  }
  if (draft.permissions.length === 0) {
    return { ok: false, message: "至少选择一个要授予的权限" };
  }
  const unknown = draft.permissions.filter((permission) => !isGrantable(permission));
  if (unknown.length > 0) {
    return { ok: false, message: `权限不在可授予范围内：${unknown.join("、")}` };
  }
  const permissions = GRANTABLE_PERMISSIONS.filter((permission) =>
    draft.permissions.includes(permission),
  );
  let expiresAt: string | null = null;
  if (draft.expiresAt.trim() !== "") {
    const parsed = new Date(draft.expiresAt);
    if (Number.isNaN(parsed.getTime())) {
      return { ok: false, message: "有效期不是合法时间" };
    }
    if (parsed.getTime() <= now.getTime()) {
      return { ok: false, message: "有效期必须晚于当前时间" };
    }
    // The server requires a timezone-aware instant; the input is local time.
    expiresAt = parsed.toISOString();
  }
  if (draft.expectedRevision !== null && !isUsableRevision(draft.expectedRevision)) {
    return { ok: false, message: "revision 必须是大于 0 的整数" };
  }
  return {
    ok: true,
    body: {
      subject_id: subjectId,
      permissions,
      expires_at: expiresAt,
      expected_revision: draft.expectedRevision,
    },
  };
}

/** Awaiting-revision guard for the revoke button. */
export function buildRevokeRequest(revision: number | null): DraftResult<RevokeBody> {
  if (!isUsableRevision(revision)) {
    return { ok: false, message: "缺少可用的 revision，请先刷新授权列表" };
  }
  return { ok: true, body: { expected_revision: revision } };
}
