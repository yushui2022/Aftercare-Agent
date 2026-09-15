// Mirrors the public operator projections in aftercare_agent/api/app.py.
// No tenant, lease-owner, fencing-token or evidence-hash field is ever sent.

export type CaseStatus = "OPEN" | "IN_REVIEW" | "CLOSED";

export type RunState =
  | "READY"
  | "RUNNING"
  | "WAITING_INPUT"
  | "WAITING_APPROVAL"
  | "RETRY_AT"
  | "REVIEW"
  | "COMPLETED"
  | "CANCELLED";

export interface CaseSummary {
  case_id: string;
  order_id: string;
  status: CaseStatus;
  version: number;
  created_at: string;
  permissions: string[];
}

export interface CaseListResponse {
  cases: CaseSummary[];
  next_created_at: string | null;
  next_case_id: string | null;
}

export interface RunSummary {
  run_id: string;
  state: RunState;
  input_version: number;
  wait_id: string | null;
  wait_generation: number | null;
  available_at: string | null;
  lease_until: string | null;
}

export interface CaseDetail extends CaseSummary {
  runs: RunSummary[];
}

export type ReviewDecision = "CONTINUE" | "CANCEL";

export interface Review {
  review_id: string;
  case_id: string;
  run_id: string;
  reason_code: string;
  decision: ReviewDecision | null;
  actor: string | null;
  decision_reason: string | null;
  created_at: string;
  decided_at: string | null;
  updated_at: string;
}

export type ApprovalState =
  | "PENDING"
  | "APPROVED"
  | "REJECTED"
  | "EXPIRED"
  | "CANCELLED";

export interface Approval {
  approval_id: string;
  case_id: string;
  run_id: string | null;
  action_id: string;
  decision: ApprovalState;
  actor: string | null;
  decision_reason: string | null;
  expires_at: string;
  created_at: string;
  decided_at: string | null;
  updated_at: string;
}

export interface ReviewListResponse {
  reviews: Review[];
}

export interface ApprovalListResponse {
  approvals: Approval[];
}

/**
 * One Case access grant as the administration projection returns it.
 *
 * The tenant stays a server-side scope and is never sent, so the workbench
 * cannot show or choose it.  `revision` is the optimistic-concurrency token
 * for the next replace or revoke.
 */
export interface CaseGrant {
  case_id: string;
  subject_id: string;
  permissions: string[];
  revision: number;
  granted_by: string;
  granted_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
  updated_at: string;
}

export interface CaseGrantListResponse {
  grants: CaseGrant[];
  /**
   * Closed-set permissions the caller may hand out here, and whether it may
   * write at all.  Both describe the caller's own token scopes, so the form
   * never has to guess the bound from a Case projection -- a real bearer
   * identity's projection is the intersection with a grant, which understates
   * a tenant administrator that holds no grant on this Case.
   */
  delegable: string[];
  can_administer: boolean;
}

export interface CaseEvent {
  case_seq: number;
  event_type: string;
  recorded_at: string | null;
  run_id: string | null;
  reference_id: string | null;
}
