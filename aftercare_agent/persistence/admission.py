"""Atomic case admission and idempotency primitives."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import (
    AdmissionKey,
    CaseRecord,
    ExecutionClaim,
    OpenCaseInput,
    RunRecord,
    SessionRecord,
    admission_digest,
    check_admission_replay,
)


@dataclass(frozen=True)
class AdmissionResult:
    case: CaseRecord
    session: SessionRecord
    run: RunRecord
    replayed: bool


@dataclass(frozen=True)
class SlotReservation:
    """A cross-process execution slot bound to one Run fence."""

    tenant_id: str
    case_id: str
    run_id: str
    owner: str
    fencing_token: int
    lease_until: datetime
    replayed: bool = False


@dataclass(frozen=True)
class ScheduledClaim:
    """A fair queue dispatch with both the Run fence and slot reservation."""

    claim: ExecutionClaim
    slot: SlotReservation


@dataclass(frozen=True)
class RetryReservation:
    """An idempotent debit from a Run's durable retry budget."""

    tenant_id: str
    run_id: str
    retry_key: str
    units: int
    remaining: int
    replayed: bool = False


class AdmissionRepository:
    """Create a case, session and run under one caller-owned transaction."""

    def open(
        self,
        connection: psycopg.Connection[Any],
        key: AdmissionKey,
        body: OpenCaseInput,
        *,
        case: CaseRecord,
        session: SessionRecord,
        run: RunRecord,
    ) -> AdmissionResult:
        if (case.tenant_id, case.case_id) != (key.tenant_id, session.case_id):
            raise ContractViolation(ErrorCode.FORBIDDEN, "admission scope mismatch")
        if (run.tenant_id, run.case_id) != (case.tenant_id, case.case_id):
            raise ContractViolation(ErrorCode.FORBIDDEN, "run scope mismatch")
        digest = admission_digest(key, body)
        inserted = connection.execute(
            "INSERT INTO aftercare_admissions(tenant_id,entrypoint,idempotency_key,payload_digest,"
            "case_id,session_id,run_id) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            (
                key.tenant_id,
                key.entrypoint,
                key.idempotency_key,
                digest,
                case.case_id,
                session.session_id,
                run.run_id,
            ),
        ).fetchone()
        if inserted is None:
            row = connection.execute(
                "SELECT payload_digest,case_id,session_id,run_id FROM aftercare_admissions "
                "WHERE tenant_id=%s AND entrypoint=%s "
                "AND idempotency_key=%s FOR UPDATE",
                (key.tenant_id, key.entrypoint, key.idempotency_key),
            ).fetchone()
            if row is None:
                raise ContractViolation(ErrorCode.RETRYABLE, "admission outcome is unknown")
            check_admission_replay(str(row[0]), digest)
            case_row = connection.execute(
                "SELECT tenant_id,case_id,order_id,version,status FROM aftercare_cases "
                "WHERE tenant_id=%s AND case_id=%s",
                (key.tenant_id, row[1]),
            ).fetchone()
            session_row = connection.execute(
                "SELECT tenant_id,case_id,session_id,channel FROM aftercare_sessions "
                "WHERE tenant_id=%s AND case_id=%s AND session_id=%s",
                (key.tenant_id, row[1], row[2]),
            ).fetchone()
            run_row = connection.execute(
                "SELECT tenant_id,case_id,run_id,session_id,predecessor_run_id,definition_version,"
                "input_version,state,fencing_token,lease_owner,lease_until,wait_id,wait_generation,"
                "available_at FROM aftercare_runs WHERE tenant_id=%s AND case_id=%s AND run_id=%s",
                (key.tenant_id, row[1], row[3]),
            ).fetchone()
            if case_row is None or session_row is None or run_row is None:
                raise ContractViolation(ErrorCode.RETRYABLE, "admission objects are incomplete")
            replay_case = CaseRecord.model_validate(
                dict(
                    zip(
                        ("tenant_id", "case_id", "order_id", "version", "status"),
                        case_row,
                        strict=False,
                    )
                )
            )
            replay_session = SessionRecord.model_validate(
                dict(
                    zip(
                        ("tenant_id", "case_id", "session_id", "channel"), session_row, strict=False
                    )
                )
            )
            run_fields = (
                "tenant_id",
                "case_id",
                "run_id",
                "session_id",
                "predecessor_run_id",
                "definition_version",
                "input_version",
                "state",
                "fencing_token",
                "lease_owner",
                "lease_until",
                "wait_id",
                "wait_generation",
                "available_at",
            )
            replay_run = RunRecord.model_validate(dict(zip(run_fields, run_row, strict=False)))
            return AdmissionResult(replay_case, replay_session, replay_run, True)
        connection.execute(
            "INSERT INTO aftercare_cases(tenant_id,case_id,order_id,version,status) "
            "VALUES (%s,%s,%s,%s,%s)",
            (case.tenant_id, case.case_id, case.order_id, case.version, case.status),
        )
        connection.execute(
            "INSERT INTO aftercare_sessions(tenant_id,case_id,session_id,channel) "
            "VALUES (%s,%s,%s,%s)",
            (session.tenant_id, session.case_id, session.session_id, session.channel),
        )
        connection.execute(
            "INSERT INTO aftercare_runs(tenant_id,case_id,run_id,session_id,predecessor_run_id,"
            "definition_version,input_version,state,fencing_token) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run.tenant_id,
                run.case_id,
                run.run_id,
                run.session_id,
                run.predecessor_run_id,
                run.definition_version,
                run.input_version,
                run.state,
                run.fencing_token,
            ),
        )
        return AdmissionResult(case, session, run, False)

    def configure_limit(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope_kind: str,
        scope_id: str,
        max_active_slots: int,
    ) -> None:
        """Set one durable execution limit; normally called by deployment tooling."""
        if scope_kind not in ("global", "tenant") or not scope_id:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid admission limit scope")
        if scope_kind == "global" and scope_id != "global":
            raise ContractViolation(ErrorCode.INVALID_INPUT, "global limit must use global scope")
        if type(max_active_slots) is not int or max_active_slots < 1:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "max_active_slots must be positive")
        connection.execute(
            "INSERT INTO aftercare_admission_limits(scope_kind,scope_id,max_active_slots) "
            "VALUES (%s,%s,%s) ON CONFLICT (scope_kind,scope_id) DO UPDATE "
            "SET max_active_slots=EXCLUDED.max_active_slots",
            (scope_kind, scope_id, max_active_slots),
        )

    def acquire_slot(
        self,
        connection: psycopg.Connection[Any],
        claim: ExecutionClaim,
        lease: timedelta,
    ) -> SlotReservation | None:
        """Reserve a global/tenant slot under the same transaction as a Run claim.

        No configured limit means admission is disabled and returns ``None``;
        once a limit exists, all contenders lock the limit rows in a stable
        order before counting active reservations.  The database clock, not a
        worker clock, decides whether an abandoned reservation is reclaimable.
        """
        if lease.total_seconds() <= 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "lease must be positive")
        rows = connection.execute(
            "SELECT scope_kind,scope_id,max_active_slots FROM aftercare_admission_limits "
            "WHERE (scope_kind='global' AND scope_id='global') OR "
            "(scope_kind='tenant' AND scope_id=%s) ORDER BY scope_kind FOR UPDATE",
            (claim.tenant_id,),
        ).fetchall()
        if not rows:
            return None
        limits = {(str(row[0]), str(row[1])): int(row[2]) for row in rows}
        existing = connection.execute(
            "SELECT owner,fencing_token,lease_until FROM aftercare_execution_slots "
            "WHERE tenant_id=%s AND run_id=%s FOR UPDATE",
            (claim.tenant_id, claim.run_id),
        ).fetchone()
        now_row = connection.execute("SELECT clock_timestamp()").fetchone()
        assert now_row is not None
        if existing is not None and existing[2] > now_row[0]:
            if existing[0] == claim.owner and int(existing[1]) == claim.fencing_token:
                connection.execute(
                    "UPDATE aftercare_execution_slots SET lease_until=clock_timestamp() + "
                    "(%s * interval '1 second') WHERE tenant_id=%s AND run_id=%s",
                    (lease.total_seconds(), claim.tenant_id, claim.run_id),
                )
                return SlotReservation(
                    claim.tenant_id,
                    claim.case_id,
                    claim.run_id,
                    claim.owner,
                    claim.fencing_token,
                    existing[2],
                    True,
                )
            raise ContractViolation(ErrorCode.RATE_LIMITED, "run already owns an active slot")
        if existing is not None:
            connection.execute(
                "DELETE FROM aftercare_execution_slots WHERE tenant_id=%s AND run_id=%s",
                (claim.tenant_id, claim.run_id),
            )
        connection.execute(
            "DELETE FROM aftercare_execution_slots WHERE lease_until <= clock_timestamp()"
        )
        for scope, scope_id in (("global", "global"), ("tenant", claim.tenant_id)):
            limit = limits.get((scope, scope_id))
            if limit is None:
                continue
            count = connection.execute(
                "SELECT count(*) FROM aftercare_execution_slots "
                "WHERE lease_until > clock_timestamp()"
                + (" AND tenant_id=%s" if scope == "tenant" else ""),
                (claim.tenant_id,) if scope == "tenant" else (),
            ).fetchone()
            assert count is not None
            if int(count[0]) >= limit:
                raise ContractViolation(ErrorCode.RATE_LIMITED, f"{scope} execution limit reached")
        row = connection.execute(
            "INSERT INTO aftercare_execution_slots(tenant_id,case_id,run_id,owner,"
            "fencing_token,lease_until) "
            "VALUES (%s,%s,%s,%s,%s,clock_timestamp() + (%s * interval '1 second')) "
            "RETURNING lease_until",
            (
                claim.tenant_id,
                claim.case_id,
                claim.run_id,
                claim.owner,
                claim.fencing_token,
                lease.total_seconds(),
            ),
        ).fetchone()
        assert row is not None
        return SlotReservation(
            claim.tenant_id,
            claim.case_id,
            claim.run_id,
            claim.owner,
            claim.fencing_token,
            row[0],
        )

    def renew_slot(
        self,
        connection: psycopg.Connection[Any],
        claim: ExecutionClaim,
        lease: timedelta,
    ) -> None:
        """Renew a slot only for its current Run fence; absent slots are a no-op."""
        if lease.total_seconds() <= 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "lease must be positive")
        connection.execute(
            "UPDATE aftercare_execution_slots SET lease_until=clock_timestamp() + "
            "(%s * interval '1 second') WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "AND owner=%s AND fencing_token=%s AND lease_until > clock_timestamp()",
            (
                lease.total_seconds(),
                claim.tenant_id,
                claim.case_id,
                claim.run_id,
                claim.owner,
                claim.fencing_token,
            ),
        )

    def release_slot(self, connection: psycopg.Connection[Any], claim: ExecutionClaim) -> None:
        connection.execute(
            "DELETE FROM aftercare_execution_slots WHERE tenant_id=%s AND case_id=%s "
            "AND run_id=%s AND owner=%s AND fencing_token=%s",
            (claim.tenant_id, claim.case_id, claim.run_id, claim.owner, claim.fencing_token),
        )

    def configure_retry_budget(
        self,
        connection: psycopg.Connection[Any],
        *,
        tenant_id: str,
        run_id: str,
        max_retries: int,
    ) -> None:
        if type(max_retries) is not int or max_retries < 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "max_retries must be non-negative")
        connection.execute(
            "INSERT INTO aftercare_retry_budgets(tenant_id,run_id,max_retries) VALUES (%s,%s,%s) "
            "ON CONFLICT (tenant_id,run_id) DO UPDATE SET max_retries=EXCLUDED.max_retries",
            (tenant_id, run_id, max_retries),
        )

    def reserve_retry(
        self,
        connection: psycopg.Connection[Any],
        *,
        tenant_id: str,
        run_id: str,
        retry_key: str,
        units: int = 1,
    ) -> RetryReservation:
        if not retry_key or type(units) is not int or units < 1:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid retry reservation")
        row = connection.execute(
            "SELECT units FROM aftercare_retry_reservations WHERE tenant_id=%s AND run_id=%s "
            "AND retry_key=%s",
            (tenant_id, run_id, retry_key),
        ).fetchone()
        if row is not None:
            if int(row[0]) != units:
                raise ContractViolation(ErrorCode.CONFLICT, "retry key reused with different units")
            budget = connection.execute(
                "SELECT max_retries,retries_used FROM aftercare_retry_budgets "
                "WHERE tenant_id=%s AND run_id=%s",
                (tenant_id, run_id),
            ).fetchone()
            if budget is None:
                raise ContractViolation(ErrorCode.RETRYABLE, "retry budget is not configured")
            return RetryReservation(
                tenant_id, run_id, retry_key, units, int(budget[0]) - int(budget[1]), True
            )
        budget = connection.execute(
            "SELECT max_retries,retries_used FROM aftercare_retry_budgets "
            "WHERE tenant_id=%s AND run_id=%s FOR UPDATE",
            (tenant_id, run_id),
        ).fetchone()
        if budget is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "retry budget is not configured")
        remaining = int(budget[0]) - int(budget[1])
        if remaining < units:
            raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "retry budget exhausted")
        connection.execute(
            "INSERT INTO aftercare_retry_reservations(tenant_id,run_id,retry_key,units) "
            "VALUES (%s,%s,%s,%s)",
            (tenant_id, run_id, retry_key, units),
        )
        connection.execute(
            "UPDATE aftercare_retry_budgets SET retries_used=retries_used+%s "
            "WHERE tenant_id=%s AND run_id=%s",
            (units, tenant_id, run_id),
        )
        return RetryReservation(tenant_id, run_id, retry_key, units, remaining - units)


class CaseRepository:
    def update_version(
        self,
        connection: psycopg.Connection[Any],
        tenant_id: str,
        case_id: str,
        expected_version: int,
        *,
        status: str | None = None,
    ) -> int:
        if status is None:
            row = connection.execute(
                "UPDATE aftercare_cases SET version=version+1 WHERE tenant_id=%s AND case_id=%s "
                "AND version=%s RETURNING version",
                (tenant_id, case_id, expected_version),
            ).fetchone()
        else:
            row = connection.execute(
                "UPDATE aftercare_cases SET version=version+1,status=%s WHERE tenant_id=%s "
                "AND case_id=%s AND version=%s RETURNING version",
                (status, tenant_id, case_id, expected_version),
            ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.CONFLICT, "case version changed or case missing")
        return int(row[0])
