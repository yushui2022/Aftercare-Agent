"""PostgreSQL investigation observation ledger invariants."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import (
    BuyerAssertion,
    BuyerStatement,
    InvestigationScope,
    OriginalReference,
)
from aftercare_agent.domain.runtime import CaseRecord
from aftercare_agent.persistence import (
    BuyerHistoryCursorRepository,
    Database,
    InvestigationObservationRepository,
    RunRepository,
    migrate,
)

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


@pytest.fixture()
def db() -> Iterator[Database]:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
        connection.execute(
            "DELETE FROM aftercare_investigation_egm_revocations WHERE tenant_id=%s",
            ("investigation-ledger",),
        )
        connection.execute(
            "DELETE FROM aftercare_investigation_egm_projections WHERE tenant_id=%s",
            ("investigation-ledger",),
        )
        connection.execute(
            "DELETE FROM aftercare_investigation_buyer_cursors WHERE tenant_id=%s",
            ("investigation-ledger",),
        )
        connection.execute(
            "DELETE FROM aftercare_investigation_egm_bindings WHERE tenant_id=%s",
            ("investigation-ledger",),
        )
        connection.execute(
            "DELETE FROM aftercare_investigation_observations WHERE tenant_id=%s",
            ("investigation-ledger",),
        )
        connection.execute(
            "DELETE FROM aftercare_runs WHERE tenant_id=%s", ("investigation-ledger",)
        )
        connection.execute(
            "DELETE FROM aftercare_cases WHERE tenant_id=%s", ("investigation-ledger",)
        )
        RunRepository().create_case(
            connection,
            CaseRecord(
                tenant_id="investigation-ledger",
                case_id="case-1",
                order_id="order-1",
                version=1,
            ),
        )
    try:
        yield value
    finally:
        value.close()


def _evidence(*, evidence_id: str = "evidence-1", event_id: str = "event-1") -> BuyerStatement:
    return BuyerStatement(
        tenant_id="investigation-ledger",
        case_id="case-1",
        order_id="order-1",
        evidence_id=evidence_id,
        source_id="buyer-channel",
        source_event_id=event_id,
        observed_at=NOW - timedelta(minutes=1),
        received_at=NOW,
        original=OriginalReference(
            artifact_id=f"artifact-{evidence_id}",
            sha256="a" * 64,
            excerpt="buyer says not received",
        ),
        assertion=BuyerAssertion.NOT_RECEIVED,
    )


def test_observation_put_is_idempotent_and_case_scoped(db: Database) -> None:
    scope = InvestigationScope(
        tenant_id="investigation-ledger", case_id="case-1", order_id="order-1"
    )
    evidence = _evidence()
    with db.transaction() as connection:
        repo = InvestigationObservationRepository()
        assert repo.put(connection, evidence, scope=scope) == evidence
        assert repo.put(connection, evidence, scope=scope) == evidence
        with pytest.raises(ContractViolation) as error:
            repo.put(
                connection,
                evidence.model_copy(update={"assertion": BuyerAssertion.RECEIVED}),
                scope=scope,
            )
        assert error.value.code is ErrorCode.CONFLICT
        assert repo.list_case(connection, scope=scope) == (evidence,)


def test_observation_source_event_cannot_be_rebound_and_revoke_is_persistent(
    db: Database,
) -> None:
    scope = InvestigationScope(
        tenant_id="investigation-ledger", case_id="case-1", order_id="order-1"
    )
    evidence = _evidence()
    with db.transaction() as connection:
        repo = InvestigationObservationRepository()
        repo.put(connection, evidence, scope=scope)
        rebound = _evidence(evidence_id="evidence-2")
        with pytest.raises(ContractViolation) as error:
            repo.put(connection, rebound, scope=scope)
        assert error.value.code is ErrorCode.CONFLICT
        with pytest.raises(ContractViolation) as invalid_revocation:
            repo.revoke(
                connection,
                scope=scope,
                evidence_id=evidence.evidence_id,
                revoked_at=NOW - timedelta(seconds=1),
                reason="source correction",
            )
        assert invalid_revocation.value.code is ErrorCode.EVIDENCE_REJECTED
        revoked = repo.revoke(
            connection,
            scope=scope,
            evidence_id=evidence.evidence_id,
            revoked_at=NOW + timedelta(seconds=1),
            reason="source correction",
        )
        assert revoked.revoked_at == NOW + timedelta(seconds=1)
        assert repo.list_case(connection, scope=scope) == (revoked,)


def test_concurrent_buyer_cursor_replay_converges(db: Database) -> None:
    scope = InvestigationScope(
        tenant_id="investigation-ledger", case_id="case-1", order_id="order-1"
    )
    barrier = Barrier(2)

    def advance() -> str:
        worker_database = Database.direct(db.dsn)
        with worker_database.transaction() as connection:
            repository = BuyerHistoryCursorRepository()
            assert repository.get(connection, scope=scope, source_id="buyer-channel") is None
            barrier.wait(timeout=5)
            return repository.advance(
                connection,
                scope=scope,
                source_id="buyer-channel",
                expected_message_id=None,
                next_message_id="message-1",
            ).message_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(advance) for _ in range(2)]
        outcomes = [future.result() for future in futures]
    assert outcomes == ["message-1", "message-1"]
