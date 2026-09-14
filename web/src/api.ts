import { identityHeaders, request, toError, type Identity } from "./http";
import { SseDecoder, parseCaseEvent } from "./sse";
import type {
  Approval,
  ApprovalListResponse,
  CaseDetail,
  CaseEvent,
  CaseListResponse,
  CaseStatus,
  Review,
  ReviewDecision,
  ReviewListResponse,
} from "./types";

export type { Identity } from "./http";
export { ApiError } from "./http";

function decisionInit(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // The server de-duplicates decisions by this key, so a retry after a
      // dropped response replays the first decision instead of conflicting.
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify(body),
  };
}

export interface CaseQuery {
  status?: CaseStatus;
  limit?: number;
}

export const api = {
  listCases(identity: Identity, query: CaseQuery = {}): Promise<CaseListResponse> {
    const params = new URLSearchParams();
    if (query.status !== undefined) {
      params.set("status", query.status);
    }
    params.set("limit", String(query.limit ?? 50));
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
  ): Promise<Review> {
    return request<Review>(
      identity,
      `/v1/cases/${encodeURIComponent(caseId)}/reviews/${encodeURIComponent(reviewId)}/decision`,
      decisionInit({ decision, decision_reason: reason === "" ? null : reason }),
    );
  },

  decideApproval(
    identity: Identity,
    caseId: string,
    approvalId: string,
    decision: "APPROVED" | "REJECTED",
    reason: string,
  ): Promise<Approval> {
    return request<Approval>(
      identity,
      `/v1/cases/${encodeURIComponent(caseId)}/approvals/${encodeURIComponent(approvalId)}/decision`,
      decisionInit({ decision, decision_reason: reason === "" ? null : reason }),
    );
  },
};
