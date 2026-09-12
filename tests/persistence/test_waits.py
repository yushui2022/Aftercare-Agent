# ruff: noqa: E501
"""PostgreSQL wait lifecycle and atomic wake-up checks."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from aftercare_agent.domain.protocol import ArtifactReference
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.domain.waits import InboxSignal, WaitRecord
from aftercare_agent.persistence import Database, RunRepository, WaitRepository, migrate


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


def _setup(db: Database, tenant: str, *, deadline: datetime | None = None) -> WaitRecord:
    case_id, run_id, wait_id = "case", "run", "wait"
    now = datetime.now(UTC)
    wait = WaitRecord(
        tenant_id=tenant,
        case_id=case_id,
        run_id=run_id,
        wait_id=wait_id,
        generation=1,
        kind="input",
        correlation_key="order",
        condition_version="v1",
        created_at=now - timedelta(seconds=2),
        deadline=deadline or now + timedelta(minutes=5),
        state="PENDING",
    )
    with db.transaction() as conn:
        for table in (
            "aftercare_wait_wakeups",
            "aftercare_waits",
            "aftercare_inbox",
            "aftercare_runs",
            "aftercare_cases",
        ):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id=%s", (tenant,))
        runs = RunRepository()
        runs.create_case(
            conn, CaseRecord(tenant_id=tenant, case_id=case_id, order_id="order", version=1)
        )
        runs.create_run(
            conn,
            RunRecord(
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                definition_version="v1",
                input_version=1,
            ),
        )
        claim = runs.claim(conn, tenant, run_id, "worker", timedelta(seconds=30))
        WaitRepository().register(conn, wait, claim)
        runs.transition(conn, claim, "WAITING_INPUT", wait_id=wait_id, wait_generation=1)
    return wait


def _signal(wait: WaitRecord, event_id: str = "reply") -> InboxSignal:
    return InboxSignal(
        tenant_id=wait.tenant_id,
        case_id=wait.case_id,
        run_id=wait.run_id,
        event_id=event_id,
        source_id="carrier",
        source_event_id=event_id,
        wait_id=wait.wait_id,
        generation=wait.generation,
        kind=wait.kind,
        correlation_key=wait.correlation_key,
        condition_version=wait.condition_version,
        received_at=wait.created_at + timedelta(seconds=1),
        payload=ArtifactReference(
            tenant_id=wait.tenant_id,
            case_id=wait.case_id,
            reference_id="artifact-" + event_id,
            sha256="a" * 64,
        ),
    )


def test_pending_reply_is_consumed_on_activation_and_wakes_run(db: Database) -> None:
    wait = _setup(db, "wait-pending")
    waits = WaitRepository()
    with db.transaction() as conn:
        result = waits.receive_and_resolve(conn, _signal(wait))
        assert result is not None and result.state == "PENDING"
    with db.transaction() as conn:
        result = waits.activate(conn, wait)
        assert result.state == "SATISFIED"
        run_row = conn.execute(
            "SELECT state FROM aftercare_runs WHERE tenant_id=%s AND run_id=%s",
            (wait.tenant_id, wait.run_id),
        ).fetchone()
        wake_row = conn.execute(
            "SELECT outcome FROM aftercare_wait_wakeups WHERE tenant_id=%s", (wait.tenant_id,)
        ).fetchone()
        assert run_row is not None and run_row[0] == "READY"
        assert wake_row is not None and wake_row[0] == "reply"


def test_active_reply_and_timeout_are_single_wakeup(db: Database) -> None:
    wait = _setup(db, "wait-active")
    waits = WaitRepository()
    with db.transaction() as conn:
        waits.activate(conn, wait)
        result = waits.receive_and_resolve(conn, _signal(wait))
        assert result is not None and result.state == "SATISFIED"
    with db.transaction() as conn:
        result = waits.resolve_timeout(conn, wait)
        assert result.state == "SATISFIED"
        wake_row = conn.execute(
            "SELECT count(*) FROM aftercare_wait_wakeups WHERE tenant_id=%s", (wait.tenant_id,)
        ).fetchone()
        assert wake_row is not None and wake_row[0] == 1


def test_old_generation_cannot_wake_new_wait(db: Database) -> None:
    wait = _setup(db, "wait-generation")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE aftercare_waits SET state='ACTIVE' WHERE tenant_id=%s", (wait.tenant_id,)
        )
        conn.execute(
            "UPDATE aftercare_runs SET wait_id='wait-2',wait_generation=2,state='WAITING_INPUT' WHERE tenant_id=%s",
            (wait.tenant_id,),
        )
        result = WaitRepository().receive_and_resolve(conn, _signal(wait, "old"))
        assert result is not None and result.state == "ACTIVE"
        run_row = conn.execute(
            "SELECT state,wait_id,wait_generation FROM aftercare_runs WHERE tenant_id=%s",
            (wait.tenant_id,),
        ).fetchone()
        assert run_row == ("WAITING_INPUT", "wait-2", 2)
