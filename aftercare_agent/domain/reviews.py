"""Pure contracts for durable human review decisions."""

from typing import Literal, Self

from pydantic import Field, model_validator

from .common import (
    Identifier,
    PositiveInt,
    RunScope,
    SchemaVersion,
    Sha256,
    UtcDatetime,
)

type ReviewDecision = Literal["CONTINUE", "CANCEL"]


class ReviewRequest(RunScope):
    """Immutable request for a trusted review of one Run revision.

    ``requested_by`` and all scope/authority fields are supplied by the
    trusted runtime.  A model may contribute evidence that led to the review,
    but it cannot choose the reviewer or the resulting Run state.
    """

    schema_version: SchemaVersion = 1
    review_id: Identifier
    reason_code: Identifier
    evidence_sha256: Sha256
    policy_version: Identifier
    requested_by: Identifier
    input_version: PositiveInt


class ReviewRecord(ReviewRequest):
    """Durable review request and its one immutable terminal decision."""

    decision: ReviewDecision | None = None
    reviewer: Identifier | None = None
    decision_idempotency_key: Identifier | None = None
    decision_reason: str | None = Field(default=None, max_length=2000)
    created_at: UtcDatetime
    decided_at: UtcDatetime | None = None
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def coherent_decision(self) -> Self:
        decided = self.decision is not None
        if not decided and self.decision_reason is not None:
            raise ValueError("pending review requests cannot contain a decision reason")
        if decided != (self.reviewer is not None):
            raise ValueError("terminal review decisions require a reviewer")
        if decided != (self.decision_idempotency_key is not None):
            raise ValueError("terminal review decisions require a decision key")
        if decided != (self.decided_at is not None):
            raise ValueError("terminal review decisions require decided_at")
        return self


__all__ = ["ReviewDecision", "ReviewRecord", "ReviewRequest"]
