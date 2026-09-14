"""Real PostgreSQL publisher leasing and acknowledgement tests."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.domain.protocol import ArtifactReference
from aftercare_agent.persistence import Database, EventRepository, migrate
from aftercare_agent.runtime.publisher import FakePublisher, OutboxPublisher


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    database = Database(dsn)
    with database.transaction() as connection:
        migrate(connection)
    return database


def event(tenant_id: str, *, event_id: str = "event-1", sequence: int = 1) -> DomainEvent:
    return DomainEvent(
        tenant_id=tenant_id,
        case_id="case-1",
        event_id=event_id,
        case_seq=sequence,
        event_type="case.opened",
        payload=ArtifactReference(
            tenant_id=tenant_id, case_id="case-1", reference_id="payload-1", sha256="a" * 64
        ),
        correlation_id="correlation-1",
        recorded_at=datetime.now(UTC),
    )


def test_outbox_append_rollback_is_not_publishable(db: Database) -> None:
    tenant = f"publisher-{uuid4().hex}"
    events = EventRepository()
    with pytest.raises(RuntimeError), db.transaction() as connection:
        events.append_outbox(connection, event(tenant))
        raise RuntimeError("synthetic transaction failure")
    with db.transaction() as connection:
        assert not events.claim_outbox(connection, tenant, "publisher-a", timedelta(seconds=30))


def test_publishers_skip_locked_claims_and_do_not_share_a_delivery(db: Database) -> None:
    tenant = f"publisher-{uuid4().hex}"
    events = EventRepository()
    with db.transaction() as connection:
        events.append_outbox(connection, event(tenant))
        events.append_outbox(connection, event(tenant, event_id="event-2", sequence=2))
    # Keep the first claim's transaction open: the second connection must
    # select the other row without waiting for this uncommitted lease.
    with db.transaction() as first_connection:
        first = events.claim_outbox(first_connection, tenant, "publisher-a", timedelta(seconds=30))
        with db.transaction() as second_connection:
            second = events.claim_outbox(
                second_connection, tenant, "publisher-b", timedelta(seconds=30)
            )
        assert len(first) == len(second) == 1
        assert first[0].event_id != second[0].event_id
        assert first[0].attempts == second[0].attempts == 1
    with db.transaction() as connection:
        assert not events.claim_outbox(connection, tenant, "publisher-c", timedelta(seconds=30))


def test_retry_backoff_then_ack_are_persistent(db: Database) -> None:
    tenant = f"publisher-{uuid4().hex}"
    events = EventRepository()
    expected = event(tenant)
    with db.transaction() as connection:
        events.append_outbox(connection, expected)
        first = events.claim_outbox(connection, tenant, "publisher-a", timedelta(seconds=30))[0]
    with db.transaction() as connection:
        assert events.retry_outbox(
            connection, first, error="provider_unavailable", delay=timedelta(hours=1)
        )
    with db.transaction() as connection:
        assert not events.claim_outbox(connection, tenant, "publisher-b", timedelta(seconds=30))
        state = connection.execute(
            "SELECT delivery_state,last_error,delivery_attempts FROM aftercare_outbox "
            "WHERE tenant_id=%s",
            (tenant,),
        ).fetchone()
        assert state == ("PENDING", "provider_unavailable", 1)
        connection.execute(
            "UPDATE aftercare_outbox SET next_attempt_at=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s",
            (tenant,),
        )
        second = events.claim_outbox(connection, tenant, "publisher-b", timedelta(seconds=30))[0]
        assert second.event == expected
        assert second.attempts == 2
    with db.transaction() as connection:
        assert events.ack_outbox(connection, second)
        assert not events.ack_outbox(connection, second)
    with db.transaction() as connection:
        assert not events.claim_outbox(connection, tenant, "publisher-c", timedelta(seconds=30))


def test_expired_same_owner_reclaim_fences_the_previous_attempt(db: Database) -> None:
    tenant = f"publisher-{uuid4().hex}"
    events = EventRepository()
    with db.transaction() as connection:
        events.append_outbox(connection, event(tenant))
        stale = events.claim_outbox(connection, tenant, "publisher-a", timedelta(seconds=30))[0]
    with db.transaction() as connection:
        connection.execute(
            "UPDATE aftercare_outbox SET delivery_lease_until=clock_timestamp() - "
            "interval '1 second' "
            "WHERE tenant_id=%s",
            (tenant,),
        )
        current = events.claim_outbox(connection, tenant, "publisher-a", timedelta(seconds=30))[0]
        assert current.attempts == stale.attempts + 1
    with db.transaction() as connection:
        with pytest.raises(ContractViolation) as error:
            events.ack_outbox(connection, stale)
        assert error.value.code is ErrorCode.LEASE_LOST
        with pytest.raises(ContractViolation) as error:
            events.retry_outbox(connection, stale, error="stale")
        assert error.value.code is ErrorCode.LEASE_LOST
        assert events.ack_outbox(connection, current)


def test_publish_before_ack_crash_is_at_least_once_not_exactly_once(db: Database) -> None:
    tenant = f"publisher-{uuid4().hex}"
    events = EventRepository()
    fake = FakePublisher()
    with db.transaction() as connection:
        events.append_outbox(connection, event(tenant))
        delivery = events.claim_outbox(connection, tenant, "publisher-a", timedelta(seconds=30))[0]
    fake.publish(delivery.event)
    # Simulate the process dying after the external side received the event
    # but before its SQL ack committed; lease expiry makes it publishable.
    with db.transaction() as connection:
        connection.execute(
            "UPDATE aftercare_outbox SET delivery_lease_until=clock_timestamp() - "
            "interval '1 second' "
            "WHERE tenant_id=%s",
            (tenant,),
        )
    result = OutboxPublisher(db).publish_once(tenant_id=tenant, owner="publisher-b", publisher=fake)
    assert result.claimed == result.acknowledged == 1
    assert result.retried == 0
    assert fake.events == [delivery.event, delivery.event]


def test_publisher_failure_is_retried_and_io_holds_no_outbox_lock(db: Database) -> None:
    tenant = f"publisher-{uuid4().hex}"
    with db.transaction() as connection:
        EventRepository().append_outbox(connection, event(tenant))

    class ProbePublisher(FakePublisher):
        def publish(self, incoming: DomainEvent) -> None:
            with db.transaction() as connection:
                row = connection.execute(
                    "SELECT delivery_state FROM aftercare_outbox "
                    "WHERE tenant_id=%s AND event_id=%s FOR UPDATE NOWAIT",
                    (incoming.tenant_id, incoming.event_id),
                ).fetchone()
                assert row == ("CLAIMED",)
            super().publish(incoming)

    fake = ProbePublisher(fail_first=1)
    runner = OutboxPublisher(db)
    first = runner.publish_once(
        tenant_id=tenant, owner="publisher-a", publisher=fake, retry_delay=timedelta(0)
    )
    assert (first.claimed, first.acknowledged, first.retried) == (1, 0, 1)
    second = runner.publish_once(tenant_id=tenant, owner="publisher-b", publisher=fake)
    assert (second.claimed, second.acknowledged, second.retried) == (1, 1, 0)
    assert len(fake.events) == 1


def test_ack_rollback_keeps_delivery_recoverable(db: Database) -> None:
    tenant = f"publisher-{uuid4().hex}"
    events = EventRepository()
    with db.transaction() as connection:
        events.append_outbox(connection, event(tenant))
        delivery = events.claim_outbox(connection, tenant, "publisher-a", timedelta(seconds=30))[0]
    with pytest.raises(RuntimeError), db.transaction() as connection:
        assert events.ack_outbox(connection, delivery)
        raise RuntimeError("synthetic ack transaction crash")
    with db.transaction() as connection:
        row = connection.execute(
            "SELECT delivery_state,delivered_at FROM aftercare_outbox WHERE tenant_id=%s", (tenant,)
        ).fetchone()
        assert row == ("CLAIMED", None)
        assert events.ack_outbox(connection, delivery)


def test_invalid_claim_and_retry_parameters_do_not_mutate_delivery(db: Database) -> None:
    tenant = f"publisher-{uuid4().hex}"
    events = EventRepository()
    with db.transaction() as connection:
        events.append_outbox(connection, event(tenant))
        with pytest.raises(ContractViolation):
            events.claim_outbox(connection, tenant, "publisher-a", timedelta(0))
        with pytest.raises(ContractViolation):
            events.claim_outbox(connection, tenant, "publisher-a", timedelta(seconds=30), limit=0)
        with pytest.raises(ContractViolation):
            events.claim_outbox(connection, tenant, "publisher-a", timedelta(seconds=30), limit=501)
        with pytest.raises(ContractViolation):
            events.claim_outbox(
                connection, tenant, "publisher-a", timedelta(seconds=30), limit=True
            )
        delivery = events.claim_outbox(connection, tenant, "publisher-a", timedelta(seconds=30))[0]
        with pytest.raises(ContractViolation):
            events.retry_outbox(
                connection, delivery, error="bad_delay", delay=timedelta(seconds=-1)
            )
        assert events.ack_outbox(connection, delivery)
