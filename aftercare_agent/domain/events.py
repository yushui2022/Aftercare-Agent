"""Case-local event identity and ordering; not a broker or durable consumer."""

from typing import Literal, Self

from pydantic import model_validator

from .common import (
    CaseScope,
    ContractViolation,
    ErrorCode,
    Identifier,
    NonNegativeInt,
    PositiveInt,
    SchemaVersion,
    UtcDatetime,
    require_same_case,
)
from .protocol import ArtifactReference

type EventType = Literal[
    "case.opened",
    "input.received",
    "run.state_changed",
    "wait.resolved",
    "investigation.proposed",
    "approval.requested",
    "approval.decided",
    "approval.expired",
    "review.requested",
    "review.decided",
    "case_grant.granted",
    "case_grant.revoked",
]


class DomainEventDraft(CaseScope):
    """Event metadata before the database assigns the case-local sequence."""

    schema_version: SchemaVersion = 1
    event_id: Identifier
    event_type: EventType
    payload_schema_version: SchemaVersion = 1
    payload: ArtifactReference
    run_id: Identifier | None = None
    correlation_id: Identifier
    causation_id: Identifier | None = None
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def coherent_draft(self) -> Self:
        require_same_case(self, self.payload)
        if (
            self.event_type
            in (
                "run.state_changed",
                "wait.resolved",
                "investigation.proposed",
                "review.requested",
                "review.decided",
            )
            and self.run_id is None
        ):
            raise ValueError("run event requires run_id")
        return self


class DomainEvent(DomainEventDraft):
    schema_version: SchemaVersion = 1
    event_id: Identifier
    case_seq: PositiveInt


class ProjectionPosition(CaseScope):
    consumer_id: Identifier
    last_case_seq: NonNegativeInt


__all__ = [
    "DomainEvent",
    "DomainEventDraft",
    "EventType",
    "ProjectionPosition",
    "consumer_application_key",
    "projection_decision",
]


def consumer_application_key(consumer: str, event: DomainEvent) -> tuple[str, str, str, str]:
    return event.tenant_id, event.case_id, consumer, event.event_id


def projection_decision(
    position: ProjectionPosition,
    incoming: DomainEvent,
    *,
    applied_at_sequence: DomainEvent | None,
) -> Literal["apply", "buffer_gap", "replay"]:
    """Caller must also enforce event_id uniqueness; lookup is not implemented here."""
    require_same_case(position, incoming)
    if incoming.case_seq <= position.last_case_seq:
        if applied_at_sequence is None or applied_at_sequence != incoming:
            raise ContractViolation(ErrorCode.CONFLICT, "sequence occupied by a different event")
        return "replay"
    if applied_at_sequence is not None:
        raise ContractViolation(
            ErrorCode.CONFLICT, "projection position and applied index disagree"
        )
    if incoming.case_seq != position.last_case_seq + 1:
        return "buffer_gap"
    return "apply"
