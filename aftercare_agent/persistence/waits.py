# ruff: noqa: E501
"""Durable wait lifecycle and atomic wake-up operations.

All methods are intended to run inside a caller transaction.  They take locks in
the runtime order Case -> Run -> Wait; the inbox row is only touched after those
locks have been acquired.  A broker may therefore acknowledge a signal only
after the surrounding transaction commits.
"""

import json
from typing import Any

import psycopg

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.domain.runtime import ExecutionClaim
from aftercare_agent.domain.waits import InboxSignal, WaitRecord, matches_wait

from .events import EventRepository

_WAIT_FIELDS = (
    "tenant_id",
    "case_id",
    "run_id",
    "wait_id",
    "generation",
    "kind",
    "correlation_key",
    "condition_version",
    "created_at",
    "deadline",
    "state",
    "resolved_by_event_id",
)


def _wait(row: tuple[Any, ...]) -> WaitRecord:
    return WaitRecord.model_validate(dict(zip(_WAIT_FIELDS, row, strict=False)))


class WaitRepository:
    """PostgreSQL implementation of the v1 wait contract."""

    def _lock(
        self, conn: psycopg.Connection[Any], wait: WaitRecord
    ) -> tuple[WaitRecord, tuple[Any, ...]]:
        case = conn.execute(
            "SELECT 1 FROM aftercare_cases WHERE tenant_id=%s AND case_id=%s FOR UPDATE",
            (wait.tenant_id, wait.case_id),
        ).fetchone()
        if case is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case not found")
        run = conn.execute(
            "SELECT state,wait_id,wait_generation FROM aftercare_runs "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s FOR UPDATE",
            (wait.tenant_id, wait.case_id, wait.run_id),
        ).fetchone()
        if run is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "run not found")
        row = conn.execute(
            "SELECT " + ",".join(_WAIT_FIELDS) + " FROM aftercare_waits "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s AND wait_id=%s AND generation=%s FOR UPDATE",
            (wait.tenant_id, wait.case_id, wait.run_id, wait.wait_id, wait.generation),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "wait not found")
        return _wait(row), run

    @staticmethod
    def _claim_valid(conn: psycopg.Connection[Any], claim: ExecutionClaim) -> None:
        case = conn.execute(
            "SELECT 1 FROM aftercare_cases WHERE tenant_id=%s AND case_id=%s FOR UPDATE",
            (claim.tenant_id, claim.case_id),
        ).fetchone()
        if case is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case not found")
        row = conn.execute(
            "SELECT state,lease_owner,fencing_token,lease_until FROM aftercare_runs "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s FOR UPDATE",
            (claim.tenant_id, claim.case_id, claim.run_id),
        ).fetchone()
        if (
            row is None
            or row[0] != "RUNNING"
            or row[1] != claim.owner
            or row[2] != claim.fencing_token
        ):
            raise ContractViolation(ErrorCode.LEASE_LOST, "execution lease lost")
        if (
            conn.execute(
                "SELECT 1 FROM aftercare_runs WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
                "AND lease_until > clock_timestamp()",
                (claim.tenant_id, claim.case_id, claim.run_id),
            ).fetchone()
            is None
        ):
            raise ContractViolation(ErrorCode.LEASE_LOST, "execution lease lost")

    def register(
        self, conn: psycopg.Connection[Any], wait: WaitRecord, claim: ExecutionClaim
    ) -> bool:
        """Persist stable PENDING intent while the run still owns its lease."""
        if (wait.tenant_id, wait.case_id, wait.run_id) != (
            claim.tenant_id,
            claim.case_id,
            claim.run_id,
        ):
            raise ContractViolation(ErrorCode.FORBIDDEN, "wait scope does not match claim")
        if wait.state != "PENDING":
            raise ContractViolation(ErrorCode.INVALID_INPUT, "new waits must start PENDING")
        self._claim_valid(conn, claim)
        bound = conn.execute(
            "SELECT wait_id,wait_generation FROM aftercare_runs WHERE tenant_id=%s AND case_id=%s "
            "AND run_id=%s FOR UPDATE",
            (claim.tenant_id, claim.case_id, claim.run_id),
        ).fetchone()
        if bound is None or (bound[0] is not None and bound != (wait.wait_id, wait.generation)):
            raise ContractViolation(ErrorCode.CONFLICT, "run already has another wait bound")
        inserted = conn.execute(
            "INSERT INTO aftercare_waits(" + ",".join(_WAIT_FIELDS) + ") "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
            (
                wait.tenant_id,
                wait.case_id,
                wait.run_id,
                wait.wait_id,
                wait.generation,
                wait.kind,
                wait.correlation_key,
                wait.condition_version,
                wait.created_at,
                wait.deadline,
                wait.state,
                wait.resolved_by_event_id,
            ),
        ).fetchone()
        if inserted is not None:
            return True
        existing = conn.execute(
            "SELECT " + ",".join(_WAIT_FIELDS) + " FROM aftercare_waits WHERE tenant_id=%s "
            "AND case_id=%s AND run_id=%s AND wait_id=%s AND generation=%s FOR UPDATE",
            (wait.tenant_id, wait.case_id, wait.run_id, wait.wait_id, wait.generation),
        ).fetchone()
        if existing is None or _wait(existing) != wait:
            raise ContractViolation(ErrorCode.CONFLICT, "wait registration replay changed")
        return False

    def activate(
        self, conn: psycopg.Connection[Any], wait: WaitRecord, *, event: DomainEvent | None = None
    ) -> WaitRecord:
        """Activate a pending wait and immediately consume an early committed reply."""
        current, run = self._lock(conn, wait)
        if current.state in ("SATISFIED", "TIMED_OUT", "CANCELLED"):
            return current
        if run[0] not in ("WAITING_INPUT", "WAITING_APPROVAL"):
            raise ContractViolation(ErrorCode.CONFLICT, "run is not waiting")
        if run[1:] != (wait.wait_id, wait.generation):
            raise ContractViolation(ErrorCode.CONFLICT, "run wait binding changed")
        if current.state == "PENDING":
            conn.execute(
                "UPDATE aftercare_waits SET state='ACTIVE' WHERE tenant_id=%s AND case_id=%s "
                "AND run_id=%s AND wait_id=%s AND generation=%s",
                (wait.tenant_id, wait.case_id, wait.run_id, wait.wait_id, wait.generation),
            )
            current = current.model_copy(update={"state": "ACTIVE"})
        return self._settle_if_ready(conn, current, run, event=event)

    def receive_and_resolve(
        self,
        conn: psycopg.Connection[Any],
        signal: InboxSignal,
        *,
        event: DomainEvent | None = None,
    ) -> WaitRecord | None:
        """Insert an authenticated signal and settle its active wait in this transaction."""
        case = conn.execute(
            "SELECT 1 FROM aftercare_cases WHERE tenant_id=%s AND case_id=%s FOR UPDATE",
            (signal.tenant_id, signal.case_id),
        ).fetchone()
        if case is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case not found")
        run = conn.execute(
            "SELECT state,wait_id,wait_generation FROM aftercare_runs WHERE tenant_id=%s AND case_id=%s "
            "AND run_id=%s FOR UPDATE",
            (signal.tenant_id, signal.case_id, signal.run_id),
        ).fetchone()
        if run is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "run not found")
        row = conn.execute(
            "SELECT "
            + ",".join(_WAIT_FIELDS)
            + " FROM aftercare_waits WHERE tenant_id=%s AND case_id=%s "
            "AND run_id=%s AND wait_id=%s AND generation=%s FOR UPDATE",
            (signal.tenant_id, signal.case_id, signal.run_id, signal.wait_id, signal.generation),
        ).fetchone()
        # Once the Case and Run are locked, lock the current Wait before
        # touching Inbox rows.  This is the same order used by timeout scans.
        EventRepository().receive_inbox(conn, signal)
        if row is None:
            return None
        current = _wait(row)
        if run[0] not in ("WAITING_INPUT", "WAITING_APPROVAL") or run[1:] != (
            signal.wait_id,
            signal.generation,
        ):
            return current
        return self._settle_if_ready(conn, current, run, event=event)

    def resolve_locked(
        self,
        conn: psycopg.Connection[Any],
        wait: WaitRecord,
        run: tuple[Any, ...],
        signal: InboxSignal,
        *,
        event: DomainEvent | None = None,
    ) -> WaitRecord:
        """Apply a signal after the caller already holds Case→Run→Wait locks.

        This narrow entry point lets an approval transaction acquire Action
        and Approval locks after the Wait, then settle the wait without
        re-entering the lock order in reverse.
        """

        if not matches_wait(wait, signal):
            raise ContractViolation(ErrorCode.CONFLICT, "signal does not match locked wait")
        EventRepository().receive_inbox(conn, signal)
        return self._settle_if_ready(conn, wait, run, event=event)

    def resolve_timeout(
        self, conn: psycopg.Connection[Any], wait: WaitRecord, *, event: DomainEvent | None = None
    ) -> WaitRecord:
        """Settle reply-first or timeout after locking the current generation."""
        current, run = self._lock(conn, wait)
        if run[0] not in ("WAITING_INPUT", "WAITING_APPROVAL") or run[1:] != (
            wait.wait_id,
            wait.generation,
        ):
            return current
        return self._settle_if_ready(conn, current, run, event=event)

    def _settle_if_ready(
        self,
        conn: psycopg.Connection[Any],
        wait: WaitRecord,
        run: tuple[Any, ...] | None = None,
        event: DomainEvent | None = None,
    ) -> WaitRecord:
        if wait.state != "ACTIVE":
            return wait
        now_row = conn.execute("SELECT clock_timestamp()").fetchone()
        assert now_row is not None
        now = now_row[0]
        rows = conn.execute(
            "SELECT payload FROM aftercare_inbox WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "AND wait_id=%s AND generation=%s AND kind=%s AND correlation_key=%s AND condition_version=%s "
            "AND received_at >= %s AND received_at <= %s ORDER BY received_at,event_id FOR UPDATE",
            (
                wait.tenant_id,
                wait.case_id,
                wait.run_id,
                wait.wait_id,
                wait.generation,
                wait.kind,
                wait.correlation_key,
                wait.condition_version,
                wait.created_at,
                now,
            ),
        ).fetchall()
        winner: InboxSignal | None = None
        for (raw,) in rows:
            candidate = InboxSignal.model_validate_json(json.dumps(raw, ensure_ascii=False))
            if matches_wait(wait, candidate):
                winner = candidate
                break
        if winner is None and now < wait.deadline:
            return wait
        outcome = "reply" if winner is not None else "timeout"
        resolved_id = winner.event_id if winner is not None else None
        conn.execute(
            "UPDATE aftercare_waits SET state=%s,resolved_by_event_id=%s WHERE tenant_id=%s AND case_id=%s "
            "AND run_id=%s AND wait_id=%s AND generation=%s",
            (
                "SATISFIED" if winner else "TIMED_OUT",
                resolved_id,
                wait.tenant_id,
                wait.case_id,
                wait.run_id,
                wait.wait_id,
                wait.generation,
            ),
        )
        conn.execute(
            "INSERT INTO aftercare_wait_wakeups(tenant_id,case_id,run_id,wait_id,generation,winner_event_id,outcome) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                wait.tenant_id,
                wait.case_id,
                wait.run_id,
                wait.wait_id,
                wait.generation,
                resolved_id,
                outcome,
            ),
        )
        conn.execute(
            "UPDATE aftercare_runs SET state='READY',lease_owner=NULL,lease_until=NULL,wait_id=NULL,"
            "wait_generation=NULL,available_at=NULL WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "AND state IN ('WAITING_INPUT','WAITING_APPROVAL') AND wait_id=%s AND wait_generation=%s",
            (wait.tenant_id, wait.case_id, wait.run_id, wait.wait_id, wait.generation),
        )
        if event is not None:
            if (
                event.event_type != "wait.resolved"
                or event.run_id != wait.run_id
                or event.tenant_id != wait.tenant_id
                or event.case_id != wait.case_id
            ):
                raise ContractViolation(
                    ErrorCode.INVALID_INPUT, "wait settlement needs wait.resolved event"
                )
            EventRepository().append_outbox(conn, event)
        return wait.model_copy(
            update={
                "state": "SATISFIED" if winner else "TIMED_OUT",
                "resolved_by_event_id": resolved_id,
            }
        )
