"""Pure contracts for durable human review decisions."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .common import (
    ContractModel,
    Identifier,
    NonNegativeInt,
    PositiveInt,
    RunScope,
    SchemaVersion,
    Sha256,
    UtcDatetime,
)

type ReviewDecision = Literal["CONTINUE", "CANCEL"]

MAX_OVERRIDE_DEADLINE_EXTENSION_SECONDS = 7 * 24 * 60 * 60


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


class ReviewOverrideRequest(ContractModel):
    """One operator-authorized budget/deadline change for a blocked Review.

    Values are additions to the latest checkpoint, rather than replacement
    values.  This keeps the operation monotonic and makes a stale operator
    screen unable to silently lower a Run's remaining budget.
    """

    checkpoint_version: PositiveInt
    model_calls_add: NonNegativeInt = 0
    tool_calls_add: NonNegativeInt = 0
    cost_microusd_add: NonNegativeInt = 0
    deadline_extension_seconds: Annotated[
        int,
        Field(
            strict=True,
            ge=0,
            le=MAX_OVERRIDE_DEADLINE_EXTENSION_SECONDS,
        ),
    ] = 0
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def has_effect(self) -> Self:
        if not any(
            (
                self.model_calls_add,
                self.tool_calls_add,
                self.cost_microusd_add,
                self.deadline_extension_seconds,
            )
        ):
            raise ValueError("review override must add budget or extend the deadline")
        return self


class ReviewOverrideRecord(RunScope):
    """Immutable audit record for one applied Review override."""

    review_id: Identifier
    override_id: Identifier
    checkpoint_version: PositiveInt
    model_calls_add: NonNegativeInt
    tool_calls_add: NonNegativeInt
    cost_microusd_add: NonNegativeInt
    deadline_extension_seconds: NonNegativeInt
    reason: str = Field(min_length=1, max_length=2000)
    created_by: Identifier
    idempotency_key: Identifier
    created_at: UtcDatetime


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


__all__ = [
    "MAX_OVERRIDE_DEADLINE_EXTENSION_SECONDS",
    "ReviewDecision",
    "ReviewOverrideRecord",
    "ReviewOverrideRequest",
    "ReviewRecord",
    "ReviewRequest",
]
