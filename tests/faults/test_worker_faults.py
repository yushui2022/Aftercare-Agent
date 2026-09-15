"""Fault-injection tests for the durable Worker boundary.

These tests intentionally exercise PostgreSQL rather than an in-memory lock:
the database row lock, lease expiry and fencing token are the authority when a
Worker disappears or a second Worker is delayed by contention.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import psycopg
import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.persistence import CheckpointRepository, Database, RunRepository
from aftercare_agent.runtime import run_once

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


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


def test_lock_timeout_does_not_replace_live_claim(db: Database) -> None:
    """A blocked claimant times out without changing the holder's fence."""

    tenant, case_id, run_id = "fault-lock-timeout", "case-1", "run-1"
    _seed(db, tenant, case_id, run_id)
    holder_ready = Event()
    release_holder = Event()

    def hold_claim() -> int:
        with db.transaction() as connection:
            claim = RunRepository().claim(
                connection, tenant, run_id, "worker-a", timedelta(seconds=30)
            )
            holder_ready.set()
            assert release_holder.wait(timeout=5), "test holder was not released"
            return claim.fencing_token

    def blocked_claim() -> str:
        assert holder_ready.wait(timeout=5), "holder did not acquire the row lock"
        try:
            with db.transaction() as connection:
                connection.execute("SET LOCAL lock_timeout = '100ms'")
                RunRepository().claim(connection, tenant, run_id, "worker-b", timedelta(seconds=30))
        except psycopg.errors.LockNotAvailable:
            return "timed_out"
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(hold_claim)
        contender = pool.submit(blocked_claim)
        assert contender.result(timeout=5) == "timed_out"
        release_holder.set()
        assert holder.result(timeout=5) == 1

    with db.connection() as connection:
        stored = RunRepository().get(connection, tenant, run_id)
        assert stored is not None
        assert stored.state == "RUNNING"
        assert stored.lease_owner == "worker-a"
        assert stored.fencing_token == 1


def test_lost_worker_restart_is_taken_over_and_resumes_without_old_write(
    db: Database,
) -> None:
    """A crashed Worker leaves no speculative checkpoint; the next Worker resumes safely."""

    tenant, case_id, run_id = "fault-restart", "case-1", "run-1"
    _seed(db, tenant, case_id, run_id)
    runs = RunRepository()

    # Simulate a Worker that dies after claiming but before its first durable
    # slice result.  The open connection is closed by the context manager.
    with db.transaction() as connection:
        first = runs.claim(connection, tenant, run_id, "worker-a", timedelta(seconds=30))
    with db.connection() as connection:
        assert CheckpointRepository().get_latest(connection, tenant, run_id) is None

    # A restart can reclaim only after the database lease has expired.  This
    # models a process/network failure without trusting the dead process to
    # clean up its lease.
    with db.transaction() as connection:
        connection.execute(
            "UPDATE aftercare_runs SET lease_until=clock_timestamp() - interval '1 second' "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        )

    result = run_once(
        db,
        tenant_id=tenant,
        run_id=run_id,
        owner="worker-b",
        now=NOW,
        max_steps=8,
    )
    assert result.completed
    assert result.fencing_token == first.fencing_token + 1

    # The original owner cannot write a late checkpoint or transition, even
    # though it still has the old in-memory claim object.
    with db.transaction() as connection:
        with pytest.raises(ContractViolation) as error:
            runs.renew(connection, first, timedelta(seconds=30))
        assert error.value.code is ErrorCode.LEASE_LOST
        stored = runs.get(connection, tenant, run_id)
        assert stored is not None
        assert stored.state == "COMPLETED"
        assert stored.lease_owner is None
        assert stored.fencing_token == result.fencing_token

    with db.connection() as connection:
        checkpoint = CheckpointRepository().get_latest(connection, tenant, run_id)
        assert checkpoint is not None
        assert checkpoint.saved_fencing_token == result.fencing_token
