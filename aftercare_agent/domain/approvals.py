"""Pure contracts for durable human or policy approval decisions."""

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from .common import (
    CaseScope,
    ContractViolation,
    ErrorCode,
    Identifier,
    PositiveInt,
    SchemaVersion,
    Sha256,
    UtcDatetime,
    utc,
)

type ApprovalDecision = Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"]


class ApprovalRequest(CaseScope):
    """Immutable request for approval of one exact Action parameter set."""

    schema_version: SchemaVersion = 1
    approval_id: Identifier
    action_id: Identifier
    action_parameters_sha256: Sha256
    policy_version: Identifier
    requested_by: Identifier
    expires_at: UtcDatetime
    # Optional binding to an already registered approval Wait.  Legacy callers
    # may create a standalone approval record, but only a fully bound request
    # can atomically wake a WAITING_APPROVAL Run.
    run_id: Identifier | None = None
    wait_id: Identifier | None = None
    wait_generation: PositiveInt | None = None

    @model_validator(mode="after")
    def coherent_wait_binding(self) -> Self:
        bound = (
            self.run_id is not None or self.wait_id is not None or self.wait_generation is not None
        )
        if bound and (self.run_id is None or self.wait_id is None or self.wait_generation is None):
            raise ValueError("approval wait binding must be complete")
        return self


class ApprovalRecord(ApprovalRequest):
    """Durable approval row returned by trusted persistence code."""

    decision: ApprovalDecision = "PENDING"
    approver: Identifier | None = None
    decision_idempotency_key: Identifier | None = None
    decision_reason: str | None = Field(default=None, max_length=2000)
    created_at: UtcDatetime
    decided_at: UtcDatetime | None = None
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def coherent_decision(self) -> Self:
        decided = self.decision in ("APPROVED", "REJECTED", "EXPIRED", "CANCELLED")
        if decided != (self.decided_at is not None):
            raise ValueError("terminal approval decisions require decided_at")
        if decided != (self.approver is not None):
            raise ValueError("terminal approval decisions require an approver")
        if decided != (self.decision_idempotency_key is not None):
            raise ValueError("terminal approval decisions require a decision key")
        return self


def assert_approval_usable(
    approval: ApprovalRecord,
    *,
    tenant_id: str,
    case_id: str,
    action_id: str,
    action_parameters_sha256: str,
    policy_version: str,
    db_now: datetime,
) -> None:
    """Verify the exact approved object immediately before dispatch."""

    if (approval.tenant_id, approval.case_id, approval.action_id) != (
        tenant_id,
        case_id,
        action_id,
    ):
        raise ContractViolation(ErrorCode.FORBIDDEN, "approval scope does not match action")
    if approval.action_parameters_sha256 != action_parameters_sha256:
        raise ContractViolation(ErrorCode.CONFLICT, "approval parameters changed")
    if approval.policy_version != policy_version:
        raise ContractViolation(ErrorCode.CONFLICT, "approval policy version changed")
    if approval.decision != "APPROVED":
        raise ContractViolation(ErrorCode.FORBIDDEN, "action is not approved")
    if utc(db_now) >= approval.expires_at:
        raise ContractViolation(ErrorCode.FORBIDDEN, "approval has expired")


__all__ = [
    "ApprovalDecision",
    "ApprovalRecord",
    "ApprovalRequest",
    "assert_approval_usable",
]
