"""Durable Worker integration tests; skipped without PostgreSQL."""

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

import aftercare_agent.runtime.worker as worker_module
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.domain.runtime import CaseRecord, ExecutionClaim, RunRecord
from aftercare_agent.persistence import CheckpointRepository, Database, RunRepository, migrate
from aftercare_agent.runtime import run_next, run_once
from aftercare_agent.runtime.harness import HarnessResult

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


def _seed(db: Database, tenant: str, case_id: str, run_id: str) -> None:
    with db.transaction() as connection:
        connection.execute("DELETE FROM aftercare_checkpoints WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        runs = RunRepository()
        runs.create_case(
            connection, CaseRecord(tenant_id=tenant, case_id=case_id, order_id="order-1", version=1)
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


def test_worker_persists_bounded_slice_and_resumes_to_completion(db: Database) -> None:
    tenant, case_id, run_id = "worker-tenant", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)
    first = run_once(
        db,
        tenant_id=tenant,
        run_id=run_id,
        owner="worker-a",
        now=NOW,
        max_steps=2,
    )
    assert not first.completed
    assert first.fencing_token == 1
    assert first.checkpoint.next_step == "tool"

    second = run_once(
        db,
        tenant_id=tenant,
        run_id=run_id,
        owner="worker-a",
        now=NOW,
        max_steps=8,
    )
    assert second.completed
    assert second.fencing_token == 2
    assert second.checkpoint.saved_fencing_token == 2
    with db.connection() as connection:
        stored = RunRepository().get(connection, tenant, run_id)
        assert stored is not None
        assert stored.state == "COMPLETED"


def test_expired_worker_cannot_save_after_takeover(db: Database) -> None:
    tenant, case_id, run_id = "worker-fence", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)
    first = run_once(
        db,
        tenant_id=tenant,
        run_id=run_id,
        owner="worker-a",
        now=NOW,
        max_steps=1,
    )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE aftercare_runs SET lease_until=clock_timestamp() - interval '1 second' "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        )
        takeover = RunRepository().claim(
            connection, tenant, run_id, "worker-b", timedelta(seconds=30)
        )
        assert takeover.fencing_token == first.fencing_token + 1
    with db.transaction() as connection:
        with pytest.raises(ContractViolation) as error:
            CheckpointRepository().save(
                connection,
                first.checkpoint,
                ExecutionClaim(
                    tenant_id=tenant,
                    case_id=case_id,
                    run_id=run_id,
                    owner="worker-a",
                    fencing_token=first.fencing_token,
                ),
            )
    assert error.value.code is ErrorCode.LEASE_LOST


def test_worker_can_claim_next_ready_run_and_reports_idle(db: Database) -> None:
    tenant, case_id, run_id = "worker-next", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)
    result = run_next(
        db,
        tenant_id=tenant,
        owner="worker-next",
        now=NOW,
        max_steps=8,
    )
    assert result is not None
    assert result.run_id == run_id
    assert result.completed
    assert run_next(db, tenant_id=tenant, owner="worker-next", now=NOW, max_steps=8) is None


def test_worker_heartbeat_keeps_long_slice_lease_alive(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, case_id, run_id = "worker-heartbeat", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)
    original = worker_module.run_fake_harness

    def slow_harness(
        *,
        tenant_id: str,
        case_id: str,
        run_id: str,
        checkpoint: Checkpoint | None,
        now: datetime,
        max_steps: int,
    ) -> HarnessResult:
        time.sleep(0.5)
        return original(
            tenant_id=tenant_id,
            case_id=case_id,
            run_id=run_id,
            checkpoint=checkpoint,
            now=now,
            max_steps=max_steps,
        )

    monkeypatch.setattr(worker_module, "run_fake_harness", slow_harness)
    result = run_once(
        db,
        tenant_id=tenant,
        run_id=run_id,
        owner="heartbeat-worker",
        now=NOW,
        lease=timedelta(milliseconds=200),
        heartbeat_interval=timedelta(milliseconds=30),
    )
    assert result.completed


def test_worker_refuses_slice_after_heartbeat_observes_takeover(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, case_id, run_id = "worker-heartbeat-lost", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)
    original = worker_module.run_fake_harness

    def stolen_harness(
        *,
        tenant_id: str,
        case_id: str,
        run_id: str,
        checkpoint: Checkpoint | None,
        now: datetime,
        max_steps: int,
    ) -> HarnessResult:
        with db.transaction() as connection:
            connection.execute(
                "UPDATE aftercare_runs SET lease_until=clock_timestamp() - interval '1 second' "
                "WHERE tenant_id=%s AND run_id=%s",
                (tenant, run_id),
            )
            RunRepository().claim(connection, tenant, run_id, "takeover", timedelta(seconds=30))
        time.sleep(0.15)
        return original(
            tenant_id=tenant_id,
            case_id=case_id,
            run_id=run_id,
            checkpoint=checkpoint,
            now=now,
            max_steps=max_steps,
        )

    monkeypatch.setattr(worker_module, "run_fake_harness", stolen_harness)
    with pytest.raises(ContractViolation) as error:
        run_once(
            db,
            tenant_id=tenant,
            run_id=run_id,
            owner="lost-worker",
            now=NOW,
            heartbeat_interval=timedelta(milliseconds=30),
        )
    assert error.value.code is ErrorCode.LEASE_LOST
    with db.connection() as connection:
        assert CheckpointRepository().get_latest(connection, tenant, run_id) is None
