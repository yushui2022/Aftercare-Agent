"""PostgreSQL gap-buffer and ordered projection ledger tests."""

import os
from datetime import UTC, datetime

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.domain.protocol import ArtifactReference
from aftercare_agent.persistence import Database, ProjectionRepository, migrate

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
        for table in (
            "aftercare_projection_buffer",
            "aftercare_projection_applied",
            "aftercare_projection_positions",
        ):
            connection.execute(f"DELETE FROM {table} WHERE tenant_id=%s", ("projection-tenant",))
    return value


def _event(
    sequence: int,
    event_id: str | None = None,
    *,
    tenant: str = "projection-tenant",
) -> DomainEvent:
    return DomainEvent(
        tenant_id=tenant,
        case_id="case-1",
        event_id=event_id or f"event-{sequence}",
        case_seq=sequence,
        event_type="case.opened" if sequence == 1 else "input.received",
        payload=ArtifactReference(
            tenant_id=tenant,
            case_id="case-1",
            reference_id=f"artifact-{sequence}",
            sha256=(f"{sequence:064x}"[-64:]),
        ),
        correlation_id="workflow-1",
        recorded_at=NOW,
    )


def test_gap_is_retained_then_contiguous_events_are_drained(db: Database) -> None:
    projections = ProjectionRepository()
    second = _event(2)
    first = _event(1)
    with db.transaction() as connection:
        result = projections.ingest(connection, second, consumer_id="ui-v1")
        assert result.decision == "buffer_gap"
        assert result.applied == ()
        assert result.position.last_case_seq == 0
        assert projections.buffered(
            connection, tenant_id=second.tenant_id, case_id=second.case_id, consumer_id="ui-v1"
        ) == (second,)

        result = projections.ingest(connection, first, consumer_id="ui-v1")
        assert result.decision == "apply"
        assert result.applied == (first, second)
        assert result.position.last_case_seq == 2
        assert (
            projections.buffered(
                connection, tenant_id=first.tenant_id, case_id=first.case_id, consumer_id="ui-v1"
            )
            == ()
        )

        assert projections.ingest(connection, first, consumer_id="ui-v1").decision == "replay"
        assert projections.ingest(connection, second, consumer_id="ui-v1").decision == "replay"


def test_same_sequence_or_event_id_with_changed_content_is_conflict(db: Database) -> None:
    projections = ProjectionRepository()
    first = _event(1)
    changed_sequence = _event(1, event_id="other-event")
    changed_id = _event(2, event_id="event-1")
    with db.transaction() as connection:
        projections.ingest(connection, first, consumer_id="audit-v1")
        with pytest.raises(ContractViolation) as sequence_error:
            projections.ingest(connection, changed_sequence, consumer_id="audit-v1")
        assert sequence_error.value.code == ErrorCode.CONFLICT
        with pytest.raises(ContractViolation) as id_error:
            projections.ingest(connection, changed_id, consumer_id="audit-v1")
        assert id_error.value.code == ErrorCode.CONFLICT


def test_consumers_and_tenants_have_independent_positions(db: Database) -> None:
    projections = ProjectionRepository()
    event = _event(1)
    with db.transaction() as connection:
        projections.ingest(connection, event, consumer_id="audit-v1")
        assert (
            projections.position(
                connection,
                tenant_id=event.tenant_id,
                case_id=event.case_id,
                consumer_id="audit-v1",
            )
            is not None
        )
        assert (
            projections.position(
                connection,
                tenant_id=event.tenant_id,
                case_id=event.case_id,
                consumer_id="ui-v1",
            )
            is None
        )
        other_tenant = _event(1, tenant="other-tenant")
        projections.ingest(connection, other_tenant, consumer_id="audit-v1")
        assert (
            projections.position(
                connection,
                tenant_id=other_tenant.tenant_id,
                case_id=other_tenant.case_id,
                consumer_id="audit-v1",
            )
            is not None
        )


def test_buffer_and_position_roll_back_together(db: Database) -> None:
    projections = ProjectionRepository()
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            projections.ingest(connection, _event(4), consumer_id="ui-v1")
            raise RuntimeError("simulate crash before commit")
    with db.transaction() as connection:
        assert (
            projections.position(
                connection,
                tenant_id="projection-tenant",
                case_id="case-1",
                consumer_id="ui-v1",
            )
            is None
        )
        assert (
            projections.buffered(
                connection, tenant_id="projection-tenant", case_id="case-1", consumer_id="ui-v1"
            )
            == ()
        )
