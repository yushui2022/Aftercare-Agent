"""PostgreSQL integration tests; skipped unless DATABASE_URL points at a test DB."""

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import Checkpoint, RemainingBudget
from aftercare_agent.domain.runtime import (
    AdmissionKey,
    CaseRecord,
    OpenCaseInput,
    RunRecord,
    SessionRecord,
)
from aftercare_agent.persistence import (
    AdmissionRepository,
    CaseRepository,
    CheckpointRepository,
    Database,
    RunRepository,
    migrate,
)


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


def test_claim_is_monotonic_and_old_owner_cannot_renew(db: Database) -> None:
    tenant, case_id, run_id = "it-tenant", "it-case", "it-run"
    runs = RunRepository()
    with db.transaction() as connection:
        connection.execute("DELETE FROM aftercare_checkpoints WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        runs.create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id=case_id, order_id="it-order", version=1),
        )
        runs.create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                definition_version="v1",
                input_version=1,
            ),
        )
        first = runs.claim(connection, tenant, run_id, "worker-a", timedelta(seconds=30))
    with db.transaction() as connection:
        connection.execute(
            "UPDATE aftercare_runs SET lease_until=clock_timestamp() - interval '1 second' "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        )
        second = runs.claim(connection, tenant, run_id, "worker-b", timedelta(seconds=30))
        assert second.fencing_token == first.fencing_token + 1
        with pytest.raises(ContractViolation) as error:
            runs.renew(connection, first, timedelta(seconds=30))
        assert error.value.code is ErrorCode.LEASE_LOST


def test_migrations_pin_and_reject_historical_sql_drift(db: Database) -> None:
    with db.transaction() as connection:
        original = connection.execute(
            "SELECT checksum FROM aftercare_schema_migrations WHERE version=1"
        ).fetchone()
        assert original is not None and original[0] is not None
        connection.execute(
            "UPDATE aftercare_schema_migrations SET checksum=%s WHERE version=1", ("f" * 64,)
        )
        with pytest.raises(RuntimeError, match="migration checksum changed: 1"):
            migrate(connection)
        migration = Path("aftercare_agent/persistence/migrations/001_initial.sql")
        checksum = hashlib.sha256(migration.read_bytes()).hexdigest()
        connection.execute(
            "UPDATE aftercare_schema_migrations SET checksum=%s WHERE version=1", (checksum,)
        )


def test_concurrent_claim_has_one_winner(db: Database) -> None:
    tenant, case_id, run_id = "it-race", "it-case", "it-run"
    with db.transaction() as connection:
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        RunRepository().create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id=case_id, order_id="it-order", version=1),
        )
        RunRepository().create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                definition_version="v1",
                input_version=1,
            ),
        )

    barrier = Barrier(2)

    def worker(owner: str) -> str:
        try:
            with db.transaction() as connection:
                barrier.wait()
                claim = RunRepository().claim(
                    connection, tenant, run_id, owner, timedelta(seconds=30)
                )
                return f"won:{claim.owner}:{claim.fencing_token}"
        except ContractViolation as error:
            return f"lost:{error.code.value}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(worker, ("worker-a", "worker-b")))
    assert sum(result.startswith("won:") for result in results) == 1
    assert sum(result == "lost:conflict" for result in results) == 1


def test_checkpoint_round_trip_is_scoped(db: Database) -> None:
    runs, checkpoints = RunRepository(), CheckpointRepository()
    case = CaseRecord(
        tenant_id="it-tenant-2", case_id="it-case-2", order_id="it-order-2", version=1
    )
    run = RunRecord(
        tenant_id=case.tenant_id,
        case_id=case.case_id,
        run_id="it-run-2",
        definition_version="v1",
        input_version=1,
    )
    checkpoint = Checkpoint(
        tenant_id=case.tenant_id,
        case_id=case.case_id,
        run_id=run.run_id,
        checkpoint_version=1,
        input_version=1,
        case_version=1,
        saved_fencing_token=1,
        definition_version="v1",
        policy_version="p1",
        tool_schema_version="t1",
        model_config_version="m1",
        protocol_version="p1",
        remaining_budget=RemainingBudget(
            model_calls=1, tool_calls=1, cost_microusd=1, deadline=datetime(2026, 9, 9, tzinfo=UTC)
        ),
        next_step="complete",
    )
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM aftercare_checkpoints WHERE tenant_id=%s", (case.tenant_id,)
        )
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (case.tenant_id,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (case.tenant_id,))
        runs.create_case(connection, case)
        runs.create_run(connection, run)
        # A checkpoint is only valid for a run holding a positive fence.
        claim = runs.claim(connection, case.tenant_id, run.run_id, "worker", timedelta(seconds=30))
        checkpoints.save(connection, checkpoint, claim)
        assert checkpoints.get_latest(connection, case.tenant_id, run.run_id) == checkpoint


def test_admission_replay_returns_original_objects_and_rejects_changed_payload(
    db: Database,
) -> None:
    key = AdmissionKey(tenant_id="it-admission", idempotency_key="idem-1")
    body = OpenCaseInput(
        order_id="order-1",
        channel="synthetic",
        message_ref="message-1",
        message_sha256="b" * 64,
    )
    case = CaseRecord(tenant_id=key.tenant_id, case_id="case-1", order_id=body.order_id, version=1)
    session = SessionRecord(
        tenant_id=key.tenant_id, case_id=case.case_id, session_id="session-1", channel="synthetic"
    )
    run = RunRecord(
        tenant_id=key.tenant_id,
        case_id=case.case_id,
        run_id="run-1",
        session_id=session.session_id,
        definition_version="v1",
        input_version=1,
    )
    admissions = AdmissionRepository()
    with db.transaction() as connection:
        connection.execute("DELETE FROM aftercare_admissions WHERE tenant_id=%s", (key.tenant_id,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (key.tenant_id,))
        connection.execute("DELETE FROM aftercare_sessions WHERE tenant_id=%s", (key.tenant_id,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (key.tenant_id,))
        first = admissions.open(connection, key, body, case=case, session=session, run=run)
        replay = admissions.open(connection, key, body, case=case, session=session, run=run)
        assert not first.replayed
        assert replay.replayed
        assert replay.case.case_id == case.case_id
        with pytest.raises(ContractViolation) as error:
            admissions.open(
                connection,
                key,
                body.model_copy(update={"message_sha256": "c" * 64}),
                case=case,
                session=session,
                run=run,
            )
        assert error.value.code is ErrorCode.CONFLICT


def test_case_version_update_is_compare_and_swap(db: Database) -> None:
    tenant = "it-version"
    with db.transaction() as connection:
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        case = CaseRecord(tenant_id=tenant, case_id="case-1", order_id="order-1", version=1)
        RunRepository().create_case(connection, case)
        cases = CaseRepository()
        assert cases.update_version(connection, tenant, case.case_id, 1) == 2
        with pytest.raises(ContractViolation) as error:
            cases.update_version(connection, tenant, case.case_id, 1)
        assert error.value.code is ErrorCode.CONFLICT
