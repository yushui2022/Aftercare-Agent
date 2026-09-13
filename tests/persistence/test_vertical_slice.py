"""A reproducible admission -> wait -> wake -> resume acceptance test."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from aftercare_agent.persistence import Database, RunRepository, migrate
from aftercare_agent.runtime import SyntheticAftercareFlow, SyntheticCase


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


def test_synthetic_aftercare_survives_worker_stop_and_wakeup(db: Database) -> None:
    case = SyntheticCase(
        tenant_id="vertical-slice",
        case_id="case-1",
        order_id="order-1",
        session_id="session-1",
        run_id="run-1",
    )
    with db.transaction() as conn:
        conn.execute("DELETE FROM aftercare_actions WHERE tenant_id=%s", (case.tenant_id,))
        conn.execute("DELETE FROM aftercare_wait_wakeups WHERE tenant_id=%s", (case.tenant_id,))
        conn.execute("DELETE FROM aftercare_waits WHERE tenant_id=%s", (case.tenant_id,))
        conn.execute("DELETE FROM aftercare_inbox WHERE tenant_id=%s", (case.tenant_id,))
        conn.execute("DELETE FROM aftercare_checkpoints WHERE tenant_id=%s", (case.tenant_id,))
        conn.execute("DELETE FROM aftercare_execution_queue WHERE tenant_id=%s", (case.tenant_id,))
        conn.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (case.tenant_id,))
        conn.execute("DELETE FROM aftercare_sessions WHERE tenant_id=%s", (case.tenant_id,))
        conn.execute("DELETE FROM aftercare_admissions WHERE tenant_id=%s", (case.tenant_id,))
        conn.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (case.tenant_id,))

    flow = SyntheticAftercareFlow(db, case)
    flow.admit()
    now = datetime.now(UTC) - timedelta(seconds=2)
    parked = flow.pause_for_customer(now=now)
    assert parked.wait.state == "ACTIVE"
    with db.transaction() as conn:
        run = RunRepository().get(conn, case.tenant_id, case.run_id)
        assert run is not None and run.state == "WAITING_INPUT"

    reply_time = datetime.now(UTC)
    resolved = flow.reply(now=reply_time, event_id="reply-1")
    assert resolved.state == "SATISFIED"
    with db.transaction() as conn:
        run = RunRepository().get(conn, case.tenant_id, case.run_id)
        assert run is not None and run.state == "READY"

    result = flow.resume(now=datetime.now(UTC))
    assert result.completed
    with db.transaction() as conn:
        run = RunRepository().get(conn, case.tenant_id, case.run_id)
        assert run is not None and run.state == "COMPLETED"

    # A retried channel delivery is an exact no-op, not a second wakeup.
    replay = flow.reply(now=reply_time, event_id="reply-1")
    assert replay.state == "SATISFIED"
