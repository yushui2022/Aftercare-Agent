"""Cross-process admission and retry-budget invariants; skipped without PostgreSQL."""

import os
from datetime import timedelta

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.persistence import AdmissionRepository, Database, RunRepository, migrate


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


def _seed(db: Database, tenant: str, case_id: str, run_id: str) -> None:
    with db.transaction() as connection:
        connection.execute("DELETE FROM aftercare_execution_slots WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_retry_reservations WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_retry_budgets WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        repo = RunRepository()
        repo.create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id=case_id, order_id="order-1", version=1),
        )
        repo.create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                definition_version="v1",
                input_version=1,
            ),
        )


def test_global_slot_is_shared_and_release_allows_next_run(db: Database) -> None:
    tenant = "admission-slots"
    _seed(db, tenant, "case-1", "run-1")
    with db.transaction() as connection:
        repo = AdmissionRepository()
        repo.configure_limit(connection, scope_kind="global", scope_id="global", max_active_slots=1)
        first = RunRepository().claim(
            connection, tenant, "run-1", "worker-a", timedelta(seconds=30)
        )
        reservation = repo.acquire_slot(connection, first, timedelta(seconds=30))
        assert reservation is not None

    with db.transaction() as connection:
        RunRepository().create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id="case-2", order_id="order-2", version=1),
        )
        RunRepository().create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
                case_id="case-2",
                run_id="run-2",
                definition_version="v1",
                input_version=1,
            ),
        )
        second = RunRepository().claim(
            connection, tenant, "run-2", "worker-b", timedelta(seconds=30)
        )
        with pytest.raises(ContractViolation) as error:
            AdmissionRepository().acquire_slot(connection, second, timedelta(seconds=30))
        assert error.value.code is ErrorCode.RATE_LIMITED
        RunRepository().transition(connection, second, "READY")

    with db.transaction() as connection:
        repo = AdmissionRepository()
        repo.release_slot(connection, first)
        second = RunRepository().claim(
            connection, tenant, "run-2", "worker-b", timedelta(seconds=30)
        )
        assert repo.acquire_slot(connection, second, timedelta(seconds=30)) is not None
        repo.release_slot(connection, second)
        connection.execute("DELETE FROM aftercare_admission_limits")


def test_retry_budget_reservation_is_idempotent_and_bounded(db: Database) -> None:
    tenant = "retry-budget"
    _seed(db, tenant, "case-1", "run-1")
    with db.transaction() as connection:
        repo = AdmissionRepository()
        repo.configure_retry_budget(connection, tenant_id=tenant, run_id="run-1", max_retries=2)
        first = repo.reserve_retry(
            connection, tenant_id=tenant, run_id="run-1", retry_key="attempt-1"
        )
        replay = repo.reserve_retry(
            connection, tenant_id=tenant, run_id="run-1", retry_key="attempt-1"
        )
        assert first.remaining == 1
        assert replay.replayed and replay.remaining == 1
        repo.reserve_retry(connection, tenant_id=tenant, run_id="run-1", retry_key="attempt-2")
        with pytest.raises(ContractViolation) as error:
            repo.reserve_retry(connection, tenant_id=tenant, run_id="run-1", retry_key="attempt-3")
        assert error.value.code is ErrorCode.BUDGET_EXHAUSTED


def test_slot_replay_returns_the_renewed_expiry_and_lost_slot_fails_closed(
    db: Database,
) -> None:
    """A reserved slice must not keep its Run lease after losing capacity."""
    tenant = "admission-renewal"
    _seed(db, tenant, "case-1", "run-1")
    try:
        with db.transaction() as connection:
            admission = AdmissionRepository()
            admission.configure_limit(
                connection, scope_kind="global", scope_id="global", max_active_slots=4
            )
            claim = RunRepository().claim(
                connection, tenant, "run-1", "worker-a", timedelta(seconds=30)
            )
            first = admission.acquire_slot(connection, claim, timedelta(seconds=30))
            assert first is not None
            replay = admission.acquire_slot(connection, claim, timedelta(minutes=2))
            assert replay is not None
            assert replay.replayed
            assert replay.lease_until > first.lease_until

        # Missing reservations are a lease loss, not a silent no-op.  This
        # simulates cleanup/reconciliation removing a slot while the worker
        # still has a live Run claim.
        with db.transaction() as connection:
            connection.execute(
                "DELETE FROM aftercare_execution_slots WHERE tenant_id=%s AND run_id=%s",
                (tenant, "run-1"),
            )
        with db.transaction() as connection:
            with pytest.raises(ContractViolation) as error:
                AdmissionRepository().renew_slot(connection, claim, timedelta(seconds=30))
            assert error.value.code is ErrorCode.LEASE_LOST

        # An expired reservation must also fail closed.
        _seed(db, tenant, "case-2", "run-2")
        with db.transaction() as connection:
            claim = RunRepository().claim(
                connection, tenant, "run-2", "worker-a", timedelta(seconds=30)
            )
            admission.acquire_slot(connection, claim, timedelta(seconds=30))
            connection.execute(
                "UPDATE aftercare_execution_slots SET lease_until=clock_timestamp() - "
                "interval '1 second' WHERE tenant_id=%s AND run_id=%s",
                (tenant, "run-2"),
            )
        with db.transaction() as connection:
            with pytest.raises(ContractViolation) as error:
                AdmissionRepository().renew_slot(connection, claim, timedelta(seconds=30))
            assert error.value.code is ErrorCode.LEASE_LOST

        # A takeover installs a newer fence in the same slot row; the old
        # worker must not renew it.
        _seed(db, tenant, "case-3", "run-3")
        with db.transaction() as connection:
            old_claim = RunRepository().claim(
                connection, tenant, "run-3", "worker-a", timedelta(seconds=30)
            )
            admission.acquire_slot(connection, old_claim, timedelta(seconds=30))
            connection.execute(
                "UPDATE aftercare_runs SET lease_until=clock_timestamp() - interval '1 second' "
                "WHERE tenant_id=%s AND run_id=%s",
                (tenant, "run-3"),
            )
            connection.execute(
                "UPDATE aftercare_execution_slots SET lease_until=clock_timestamp() - "
                "interval '1 second' WHERE tenant_id=%s AND run_id=%s",
                (tenant, "run-3"),
            )
            new_claim = RunRepository().claim(
                connection, tenant, "run-3", "worker-b", timedelta(seconds=30)
            )
            replacement = admission.acquire_slot(connection, new_claim, timedelta(seconds=30))
            assert replacement is not None
        with db.transaction() as connection:
            with pytest.raises(ContractViolation) as error:
                AdmissionRepository().renew_slot(connection, old_claim, timedelta(seconds=30))
            assert error.value.code is ErrorCode.LEASE_LOST
    finally:
        with db.transaction() as connection:
            connection.execute(
                "DELETE FROM aftercare_admission_limits WHERE scope_kind='global' "
                "AND scope_id='global'"
            )
