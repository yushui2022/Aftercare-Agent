"""Durable Worker integration tests; skipped without PostgreSQL."""

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

import aftercare_agent.runtime.worker as worker_module
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.domain.runtime import CaseRecord, ExecutionClaim, RunRecord
from aftercare_agent.persistence import (
    AdmissionRepository,
    CheckpointRepository,
    Database,
    RunRepository,
    migrate,
)
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


@pytest.fixture()
def queue_db(db: Database) -> Iterator[Database]:
    """Give cross-tenant scheduler tests their own isolated queue universe."""
    schema = f"queue_test_{uuid4().hex}"
    with db.transaction() as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    value = Database(make_conninfo(db.dsn, options=f"-c search_path={schema}"))
    try:
        with value.transaction() as connection:
            migrate(connection)
        yield value
    finally:
        with db.transaction() as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


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


def test_two_independent_workers_only_one_can_advance_a_run(db: Database) -> None:
    tenant, case_id, run_id = "worker-dual", "worker-case", "worker-run"
    _seed(db, tenant, case_id, run_id)

    def execute(owner: str) -> str:
        try:
            result = run_once(
                db,
                tenant_id=tenant,
                run_id=run_id,
                owner=owner,
                now=NOW,
                max_steps=8,
            )
            return f"completed:{result.owner}"
        except ContractViolation as error:
            return f"rejected:{error.code.value}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, ("worker-a", "worker-b")))
    assert sum(item.startswith("completed:") for item in outcomes) == 1
    assert sum(item.startswith("rejected:") for item in outcomes) == 1


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


def test_shared_worker_round_robins_across_tenants(queue_db: Database) -> None:
    _seed(queue_db, "fair-tenant-a", "fair-case-a", "fair-run-a")
    _seed(queue_db, "fair-tenant-b", "fair-case-b", "fair-run-b")

    first = run_next(queue_db, tenant_id=None, owner="fair-worker", now=NOW, max_steps=1)
    second = run_next(queue_db, tenant_id=None, owner="fair-worker", now=NOW, max_steps=1)

    assert first is not None and second is not None
    assert {first.tenant_id, second.tenant_id} == {"fair-tenant-a", "fair-tenant-b"}


def test_shared_worker_reclaims_expired_inflight_queue_row(queue_db: Database) -> None:
    tenant, case_id, run_id = "fair-reclaim", "fair-case", "fair-run"
    _seed(queue_db, tenant, case_id, run_id)
    with queue_db.transaction() as connection:
        old_claim = RunRepository().claim(
            connection, tenant, run_id, "crashed-worker", timedelta(seconds=30)
        )
        connection.execute(
            "UPDATE aftercare_runs SET lease_until=clock_timestamp() - interval '1 second' "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        )
        queue = connection.execute(
            "SELECT state,claim_owner,claim_token FROM aftercare_execution_queue "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        ).fetchone()
        assert queue == ("IN_FLIGHT", old_claim.owner, old_claim.fencing_token)

    result = run_next(queue_db, tenant_id=None, owner="recovery-worker", now=NOW, max_steps=8)
    assert result is not None
    assert result.fencing_token == old_claim.fencing_token + 1
    assert result.owner == "recovery-worker"


def test_shared_worker_skips_tenant_at_its_admission_limit(queue_db: Database) -> None:
    _seed(queue_db, "limited-tenant", "limited-case", "limited-run")
    _seed(queue_db, "available-tenant", "available-case", "available-run")
    with queue_db.transaction() as connection:
        admission = AdmissionRepository()
        admission.configure_limit(
            connection,
            scope_kind="tenant",
            scope_id="limited-tenant",
            max_active_slots=1,
        )
        claim = RunRepository().claim(
            connection, "limited-tenant", "limited-run", "held-worker", timedelta(seconds=30)
        )
        assert admission.acquire_slot(connection, claim, timedelta(seconds=30)) is not None

    result = run_next(queue_db, tenant_id=None, owner="fair-worker", now=NOW, max_steps=8)
    assert result is not None
    assert result.tenant_id == "available-tenant"


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
