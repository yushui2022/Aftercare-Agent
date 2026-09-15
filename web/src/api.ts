import { identityHeaders, request, toError, type Identity } from "./http";
import type { GrantBody, RevokeBody } from "./grants";
import { SseDecoder, parseCaseEvent } from "./sse";
import type {
  Approval,
  ApprovalListResponse,
  CaseDetail,
  CaseEvent,
  CaseGrant,
  CaseGrantListResponse,
  CaseListResponse,
  CaseStatus,
  Review,
  ReviewDecision,
  ReviewListResponse,
} from "./types";

export type { Identity } from "./http";
export { ApiError } from "./http";

function decisionInit(body: unknown, idempotencyKey: string): RequestInit {
  return {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // The server de-duplicates decisions by this key, so a retry after a
      // dropped response replays the first decision instead of conflicting.
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(body),
  };
}

export interface CaseQuery {
  status?: CaseStatus;
  limit?: number;
  afterCreatedAt?: string;
  afterCaseId?: string;
}

export const api = {
  listCases(identity: Identity, query: CaseQuery = {}): Promise<CaseListResponse> {
    const params = new URLSearchParams();
    if (query.status !== undefined) {
      params.set("status", query.status);
    }
    params.set("limit", String(query.limit ?? 50));
    if (query.afterCreatedAt !== undefined) {
      params.set("after_created_at", query.afterCreatedAt);
    }
    if (query.afterCaseId !== undefined) {
      params.set("after_case_id", query.afterCaseId);
    }
    return request<CaseListResponse>(identity, `/v1/cases?${params.toString()}`);
  },

  getCase(identity: Identity, caseId: string): Promise<CaseDetail> {
    return request<CaseDetail>(identity, `/v1/cases/${encodeURIComponent(caseId)}`);
  },

  listReviews(identity: Identity, caseId: string): Promise<ReviewListResponse> {
    return request<ReviewListResponse>(
      identity,
      `/v1/cases/${encodeURIComponent(caseId)}/reviews`,
    );
  },

  listApprovals(identity: Identity, caseId: string): Promise<ApprovalListResponse> {
    return request<ApprovalListResponse>(
      identity,
      `/v1/cases/${encodeURIComponent(caseId)}/approvals`,
    );
  },

  /** One-shot replay; used when live following is paused. */
  async listEvents(identity: Identity, caseId: string): Promise<CaseEvent[]> {
    const response = await fetch(
      `/api/v1/cases/${encodeURIComponent(caseId)}/events?limit=200`,
      { headers: identityHeaders(identity), cache: "no-store" },
    );
    if (!response.ok) {
      throw await toError(response);
    }
    const decoder = new SseDecoder();
    const events: CaseEvent[] = [];
    for (const message of decoder.push(await response.text())) {
      const event = parseCaseEvent(message);
      if (event !== null) {
        events.push(event);
      }
    }
    return events;
  },

  decideReview(
    identity: Identity,
    caseId: string,
    reviewId: string,
    decision: ReviewDecision,
    reason: string,
    idempotencyKey: string = crypto.randomUUID(),
  ): Promise<Review> {
    return request<Review>(
      identity,
      `/v1/cases/${encodeURIComponent(caseId)}/reviews/${encodeURIComponent(reviewId)}/decision`,
      decisionInit({ decision, decision_reason: reason === "" ? null : reason }, idempotencyKey),
    );
  },

  decideApproval(
    identity: Identity,
    caseId: string,
    approvalId: string,
    decision: "APPROVED" | "REJECTED",
    reason: string,
    idempotencyKey: string = crypto.randomUUID(),
  ): Promise<Approval> {
    return request<Approval>(
      identity,
      `/v1/cases/${encodeURIComponent(caseId)}/approvals/${encodeURIComponent(approvalId)}/decision`,
      decisionInit({ decision, decision_reason: reason === "" ? null : reason }, idempotencyKey),
    );
  },

  /** Access rows for one Case, including revoked history, for the admin view. */
  listCaseGrants(identity: Identity, caseId: string, limit = 50): Promise<CaseGrantListResponse> {
    return request<CaseGrantListResponse>(
      identity,
      `/v1/cases/${encodeURIComponent(caseId)}/grants?limit=${String(limit)}`,
    );
  },

  /**
   * Grant or replace one subject's access to a Case.
   *
   * Optimistic concurrency takes the place of an idempotency key here: a first
   * grant omits `expected_revision`, and a replacement must carry the revision
   * the operator actually read, so a blind retry after a timeout answers
   * `409` instead of applying twice.
   */
  grantCaseAccess(identity: Identity, caseId: string, body: GrantBody): Promise<CaseGrant> {
    return request<CaseGrant>(identity, `/v1/cases/${encodeURIComponent(caseId)}/grants`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  /** Revoke one subject; the row stays for audit and revision checks. */
  revokeCaseAccess(
    identity: Identity,
    caseId: string,
    subjectId: string,
    body: RevokeBody,
  ): Promise<CaseGrant> {
    return request<CaseGrant>(
      identity,
      `/v1/cases/${encodeURIComponent(caseId)}/grants/${encodeURIComponent(subjectId)}/revoke`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
  },
};
