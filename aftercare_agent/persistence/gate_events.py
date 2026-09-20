"""Immutable outbox events for trusted approval and human-review gates."""

from datetime import UTC, datetime
from typing import Any, Literal

import psycopg

from aftercare_agent.domain.approvals import ApprovalRecord
from aftercare_agent.domain.events import DomainEvent, DomainEventDraft
from aftercare_agent.domain.protocol import ArtifactReference
from aftercare_agent.domain.reviews import ReviewOverrideRecord, ReviewRecord
from aftercare_agent.domain.strategy_migrations import StrategyMigrationRecord

from .events import EventRepository

GateEventKind = Literal[
    "approval.requested",
    "approval.decided",
    "approval.expired",
    "review.requested",
    "review.decided",
    "review.strategy_migrated",
]


def _record_event(
    connection: psycopg.Connection[Any],
    record: ApprovalRecord | ReviewRecord | StrategyMigrationRecord,
    *,
    kind: GateEventKind,
    event_id: str,
    correlation_id: str,
    causation_id: str | None = None,
) -> tuple[DomainEvent, bool]:
    snapshot = record.model_dump(mode="json")
    digest = EventRepository._snapshot_sha256(snapshot)
    reference_id = f"event-payload:{event_id}"
    draft = DomainEventDraft(
        tenant_id=record.tenant_id,
        case_id=record.case_id,
        event_id=event_id,
        event_type=kind,
        payload=ArtifactReference(
            tenant_id=record.tenant_id,
            case_id=record.case_id,
            reference_id=reference_id,
            sha256=digest,
        ),
        run_id=record.run_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
        recorded_at=datetime.now(UTC),
    )
    return EventRepository().append_event(connection, draft, snapshot=snapshot)


def append_approval_requested(
    connection: psycopg.Connection[Any], record: ApprovalRecord
) -> tuple[DomainEvent, bool]:
    return _record_event(
        connection,
        record,
        kind="approval.requested",
        event_id=f"approval:{record.case_id}:{record.approval_id}:requested",
        correlation_id=record.approval_id,
    )


def append_approval_decided(
    connection: psycopg.Connection[Any], record: ApprovalRecord
) -> tuple[DomainEvent, bool]:
    assert record.decision_idempotency_key is not None
    return _record_event(
        connection,
        record,
        kind="approval.decided" if record.decision != "EXPIRED" else "approval.expired",
        event_id=(
            f"approval:{record.case_id}:{record.approval_id}:decision:"
            f"{record.decision_idempotency_key}"
        ),
        correlation_id=record.approval_id,
    )


def append_review_requested(
    connection: psycopg.Connection[Any], record: ReviewRecord
) -> tuple[DomainEvent, bool]:
    return _record_event(
        connection,
        record,
        kind="review.requested",
        event_id=f"review:{record.case_id}:{record.review_id}:requested",
        correlation_id=record.review_id,
    )


def append_review_decided(
    connection: psycopg.Connection[Any],
    record: ReviewRecord,
    *,
    override: ReviewOverrideRecord | None = None,
) -> tuple[DomainEvent, bool]:
    assert record.decision_idempotency_key is not None
    snapshot = record.model_dump(mode="json")
    if override is not None:
        snapshot["override"] = override.model_dump(mode="json")
    digest = EventRepository._snapshot_sha256(snapshot)
    event_id = (
        f"review:{record.case_id}:{record.review_id}:decision:{record.decision_idempotency_key}"
    )
    reference_id = f"event-payload:{event_id}"
    draft = DomainEventDraft(
        tenant_id=record.tenant_id,
        case_id=record.case_id,
        event_id=event_id,
        event_type="review.decided",
        payload=ArtifactReference(
            tenant_id=record.tenant_id,
            case_id=record.case_id,
            reference_id=reference_id,
            sha256=digest,
        ),
        run_id=record.run_id,
        correlation_id=record.review_id,
        recorded_at=datetime.now(UTC),
    )
    return EventRepository().append_event(connection, draft, snapshot=snapshot)


def append_strategy_migrated(
    connection: psycopg.Connection[Any], record: StrategyMigrationRecord
) -> tuple[DomainEvent, bool]:
    return _record_event(
        connection,
        record,
        kind="review.strategy_migrated",
        event_id=f"review:{record.case_id}:strategy-migration:{record.migration_id}",
        correlation_id=record.migration_id,
    )


__all__ = [
    "append_approval_decided",
    "append_approval_requested",
    "append_review_decided",
    "append_review_requested",
    "append_strategy_migrated",
]
