"""PostgreSQL Inbox/Outbox integration tests."""

import os
from datetime import UTC, datetime

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.events import DomainEvent, DomainEventDraft
from aftercare_agent.domain.protocol import ArtifactReference
from aftercare_agent.domain.waits import InboxSignal
from aftercare_agent.persistence import Database, EventRepository, migrate

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


def _event(event_id: str = "event-1", case_seq: int = 1) -> DomainEvent:
    return DomainEvent(
        tenant_id="event-tenant",
        case_id="event-case",
        event_id=event_id,
        case_seq=case_seq,
        event_type="case.opened",
        payload=ArtifactReference(
            tenant_id="event-tenant",
            case_id="event-case",
            reference_id="artifact-1",
            sha256="a" * 64,
        ),
        correlation_id="correlation-1",
        recorded_at=NOW,
    )


def _signal(source_event_id: str = "source-event-1") -> InboxSignal:
    return InboxSignal(
        tenant_id="event-tenant",
        case_id="event-case",
        run_id="run-1",
        event_id="inbox-event-1",
        source_id="carrier-1",
        source_event_id=source_event_id,
        wait_id="wait-1",
        generation=1,
        kind="input",
        correlation_key="order-1",
        condition_version="v1",
        received_at=NOW,
        payload=ArtifactReference(
            tenant_id="event-tenant",
            case_id="event-case",
            reference_id="artifact-2",
            sha256="b" * 64,
        ),
    )


def test_outbox_and_inbox_replays_are_idempotent_but_changed_payloads_conflict(
    db: Database,
) -> None:
    events = EventRepository()
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM aftercare_inbox_applications WHERE tenant_id=%s", ("event-tenant",)
        )
        connection.execute("DELETE FROM aftercare_inbox WHERE tenant_id=%s", ("event-tenant",))
        connection.execute("DELETE FROM aftercare_outbox WHERE tenant_id=%s", ("event-tenant",))
        event = _event()
        assert events.append_outbox(connection, event)
        assert not events.append_outbox(connection, event)
        with pytest.raises(ContractViolation) as outbox_error:
            events.append_outbox(
                connection,
                _event(event_id="event-1", case_seq=1).model_copy(
                    update={"correlation_id": "changed"}
                ),
            )
        assert outbox_error.value.code is ErrorCode.CONFLICT

        signal = _signal()
        assert events.receive_inbox(connection, signal)
        assert not events.receive_inbox(connection, signal)
        with pytest.raises(ContractViolation) as inbox_error:
            events.receive_inbox(
                connection, signal.model_copy(update={"received_at": NOW.replace(hour=13)})
            )
        assert inbox_error.value.code is ErrorCode.CONFLICT


def test_each_consumer_is_deduplicated_independently(db: Database) -> None:
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM aftercare_inbox_applications WHERE tenant_id=%s", ("event-tenant",)
        )
        events = EventRepository()
        assert events.apply_once(
            connection,
            tenant_id="event-tenant",
            case_id="event-case",
            consumer_id="projection-a",
            event_id="event-1",
        )
        assert not events.apply_once(
            connection,
            tenant_id="event-tenant",
            case_id="event-case",
            consumer_id="projection-a",
            event_id="event-1",
        )
        assert events.apply_once(
            connection,
            tenant_id="event-tenant",
            case_id="event-case",
            consumer_id="projection-b",
            event_id="event-1",
        )


def test_append_event_allocates_case_sequence_and_preserves_snapshot(db: Database) -> None:
    events = EventRepository()
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM aftercare_event_payloads WHERE tenant_id=%s", ("event-auto",)
        )
        connection.execute("DELETE FROM aftercare_outbox WHERE tenant_id=%s", ("event-auto",))
        connection.execute(
            "DELETE FROM aftercare_case_event_sequences WHERE tenant_id=%s", ("event-auto",)
        )
        draft = DomainEventDraft(
            tenant_id="event-auto",
            case_id="case-1",
            event_id="approval:one:requested",
            event_type="approval.requested",
            run_id="run-1",
            payload=ArtifactReference(
                tenant_id="event-auto",
                case_id="case-1",
                reference_id="event-payload:approval:one:requested",
                sha256="a" * 64,
            ),
            correlation_id="approval-one",
            recorded_at=NOW,
        )
        snapshot = {"approval_id": "one", "decision": "PENDING"}
        # Use the repository's canonical digest instead of hand-editing the
        # event contract in this test.
        draft = draft.model_copy(
            update={
                "payload": draft.payload.model_copy(
                    update={"sha256": events._snapshot_sha256(snapshot)}
                )
            }
        )
        first, replayed = events.append_event(connection, draft, snapshot=snapshot)
        assert first.case_seq == 1 and not replayed
        second, replayed = events.append_event(connection, draft, snapshot=snapshot)
        assert second == first and replayed
        row = connection.execute(
            "SELECT payload FROM aftercare_event_payloads WHERE tenant_id=%s AND reference_id=%s",
            ("event-auto", draft.payload.reference_id),
        ).fetchone()
        assert row is not None and row[0] == snapshot

        next_draft = draft.model_copy(update={"event_id": "approval:one:decided"})
        next_draft = next_draft.model_copy(
            update={
                "event_type": "approval.decided",
                "payload": next_draft.payload.model_copy(
                    update={"reference_id": "event-payload:approval:one:decided"}
                ),
            }
        )
        third, _ = events.append_event(connection, next_draft, snapshot=snapshot)
        assert third.case_seq == 2
