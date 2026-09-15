"""Immutable outbox events for Case access-grant changes.

The grant row is the authority; these events are its audit trail.  They are
appended by the repository that performs the change, so no caller can mutate
the ACL and leave the case stream silent, and they use the same
content-addressed snapshot convention as the approval and review gate events.
"""

from datetime import UTC, datetime
from typing import Any, Literal

import psycopg

from aftercare_agent.auth.grants import CaseGrantRecord
from aftercare_agent.domain.events import DomainEvent, DomainEventDraft
from aftercare_agent.domain.protocol import ArtifactReference

from .events import EventRepository

GrantEventKind = Literal["case_grant.granted", "case_grant.revoked"]


def append_case_grant_event(
    connection: psycopg.Connection[Any],
    record: CaseGrantRecord,
    *,
    kind: GrantEventKind,
) -> tuple[DomainEvent, bool]:
    """Append the access change that produced this grant revision.

    The event id carries the kind and the revision, so two changes to the same
    subject-and-Case pair are two distinct events, while replaying one change
    maps back to the event it already produced instead of advancing the stream.
    """

    snapshot = record.model_dump(mode="json")
    digest = EventRepository._snapshot_sha256(snapshot)
    suffix = "granted" if kind == "case_grant.granted" else "revoked"
    event_id = f"case_grant:{record.case_id}:{record.subject_id}:{suffix}:{record.revision}"
    draft = DomainEventDraft(
        tenant_id=record.tenant_id,
        case_id=record.case_id,
        event_id=event_id,
        event_type=kind,
        payload=ArtifactReference(
            tenant_id=record.tenant_id,
            case_id=record.case_id,
            reference_id=f"event-payload:{event_id}",
            sha256=digest,
        ),
        correlation_id=record.subject_id,
        recorded_at=datetime.now(UTC),
    )
    return EventRepository().append_event(connection, draft, snapshot=snapshot)


__all__ = ["GrantEventKind", "append_case_grant_event"]
