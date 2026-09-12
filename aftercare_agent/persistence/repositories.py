"""Repository operations with row locks and database-clock leases."""

import json
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.domain.runtime import (
    AttemptRecord,
    CaseRecord,
    ExecutionClaim,
    RunRecord,
    SessionRecord,
    StepRecord,
    validate_transition,
)

_RUN_FIELDS = (
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


def _run(row: tuple[Any, ...]) -> RunRecord:
    return RunRecord.model_validate(dict(zip(_RUN_FIELDS, row, strict=False)))


class RunRepository:
    def create_case(self, connection: psycopg.Connection[Any], case: CaseRecord) -> None:
        connection.execute(
            "INSERT INTO aftercare_cases(tenant_id,case_id,order_id,version,status) "
            "VALUES (%s,%s,%s,%s,%s)",
            (case.tenant_id, case.case_id, case.order_id, case.version, case.status),
        )

    def create_run(self, connection: psycopg.Connection[Any], run: RunRecord) -> None:
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

    def get(
        self, connection: psycopg.Connection[Any], tenant: str, run_id: str
    ) -> RunRecord | None:
        row = connection.execute(
            "SELECT "
            + ",".join(_RUN_FIELDS)
            + " FROM aftercare_runs WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        ).fetchone()
        return None if row is None else _run(row)

    def claim(
        self,
        connection: psycopg.Connection[Any],
        tenant: str,
        run_id: str,
        owner: str,
        lease: timedelta,
    ) -> ExecutionClaim:
        row = connection.execute(
            "SELECT tenant_id,case_id,run_id,fencing_token FROM aftercare_runs "
            "WHERE tenant_id=%s AND run_id=%s AND (state='READY' OR "
            "(state='RUNNING' AND lease_until <= clock_timestamp())) FOR UPDATE",
            (tenant, run_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.CONFLICT, "run is not claimable")
        token = int(row[3]) + 1
        updated = connection.execute(
            "UPDATE aftercare_runs SET state='RUNNING', lease_owner=%s, "
            "lease_until=clock_timestamp() + (%s * interval '1 second'), fencing_token=%s "
            "WHERE tenant_id=%s AND run_id=%s "
            "RETURNING tenant_id,case_id,run_id,fencing_token",
            (owner, lease.total_seconds(), token, tenant, run_id),
        ).fetchone()
        assert updated is not None
        return ExecutionClaim(
            tenant_id=updated[0],
            case_id=updated[1],
            run_id=updated[2],
            owner=owner,
            fencing_token=updated[3],
        )

    def claim_next(
        self,
        connection: psycopg.Connection[Any],
        tenant: str,
        owner: str,
        lease: timedelta,
    ) -> ExecutionClaim | None:
        """Claim the oldest runnable READY Run without blocking other Workers."""
        row = connection.execute(
            "SELECT tenant_id,case_id,run_id,fencing_token FROM aftercare_runs "
            "WHERE tenant_id=%s AND state='READY' AND (available_at IS NULL "
            "OR available_at <= clock_timestamp()) "
            "ORDER BY available_at NULLS FIRST, run_id FOR UPDATE SKIP LOCKED LIMIT 1",
            (tenant,),
        ).fetchone()
        if row is None:
            return None
        token = int(row[3]) + 1
        updated = connection.execute(
            "UPDATE aftercare_runs SET state='RUNNING', lease_owner=%s, "
            "lease_until=clock_timestamp() + (%s * interval '1 second'), fencing_token=%s "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s AND state='READY' "
            "RETURNING tenant_id,case_id,run_id,fencing_token",
            (owner, lease.total_seconds(), token, row[0], row[1], row[2]),
        ).fetchone()
        assert updated is not None
        return ExecutionClaim(
            tenant_id=updated[0],
            case_id=updated[1],
            run_id=updated[2],
            owner=owner,
            fencing_token=updated[3],
        )

    def renew(
        self, connection: psycopg.Connection[Any], claim: ExecutionClaim, lease: timedelta
    ) -> None:
        count = connection.execute(
            "UPDATE aftercare_runs SET lease_until=clock_timestamp() + (%s * interval '1 second') "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s AND state='RUNNING' "
            "AND lease_owner=%s AND fencing_token=%s AND lease_until > clock_timestamp()",
            (
                lease.total_seconds(),
                claim.tenant_id,
                claim.case_id,
                claim.run_id,
                claim.owner,
                claim.fencing_token,
            ),
        ).rowcount
        if count != 1:
            raise ContractViolation(ErrorCode.LEASE_LOST, "execution lease lost")

    def transition(
        self,
        connection: psycopg.Connection[Any],
        claim: ExecutionClaim,
        target: str,
        *,
        case_version: int | None = None,
        expected_case_version: int | None = None,
        wait_id: str | None = None,
        wait_generation: int | None = None,
        available_at: datetime | None = None,
    ) -> RunRecord:
        """Atomically advance a claimed run and return its new trusted record.

        The Case row is locked before the Run row, matching the runtime lock
        ordering.  A transition out of RUNNING must present the current lease
        claim; the database clock is used for the expiry check.  ``case_version``
        is the preferred spelling; ``expected_case_version`` is retained as a
        compatibility alias and cannot disagree with it.
        """
        if case_version is not None and expected_case_version is not None:
            if case_version != expected_case_version:
                raise ContractViolation(ErrorCode.CONFLICT, "case version changed")
        expected = case_version if case_version is not None else expected_case_version
        if target == "RUNNING":
            raise ContractViolation(ErrorCode.CONFLICT, "use claim() to enter RUNNING")
        case_row = connection.execute(
            "SELECT version FROM aftercare_cases WHERE tenant_id=%s AND case_id=%s FOR UPDATE",
            (claim.tenant_id, claim.case_id),
        ).fetchone()
        if case_row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case not found")
        current_case_version = int(case_row[0])
        if expected is not None:
            # Constructing CaseRecord is unnecessary here; the locked scalar is
            # authoritative and the pure guard still supplies its error code.
            if type(expected) is not int or current_case_version != expected:
                raise ContractViolation(ErrorCode.CONFLICT, "case version changed")

        row = connection.execute(
            "SELECT "
            + ",".join(_RUN_FIELDS)
            + " FROM aftercare_runs WHERE tenant_id=%s AND case_id=%s AND run_id=%s FOR UPDATE",
            (claim.tenant_id, claim.case_id, claim.run_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "run not found")
        previous = _run(row)
        validate_transition(previous.state, target)  # type: ignore[arg-type]
        if previous.state == "RUNNING":
            valid = connection.execute(
                "SELECT 1 FROM aftercare_runs WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
                "AND lease_owner=%s AND fencing_token=%s AND lease_until > clock_timestamp()",
                (
                    claim.tenant_id,
                    claim.case_id,
                    claim.run_id,
                    claim.owner,
                    claim.fencing_token,
                ),
            ).fetchone()
            if valid is None:
                raise ContractViolation(ErrorCode.LEASE_LOST, "execution lease lost")
        if target in ("WAITING_INPUT", "WAITING_APPROVAL"):
            if wait_id is None or wait_generation is None or wait_generation < 1:
                raise ContractViolation(
                    ErrorCode.INVALID_INPUT, "waiting transition needs wait binding"
                )
        elif wait_id is not None or wait_generation is not None:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "wait binding is only valid for waiting"
            )
        if target == "RETRY_AT":
            if available_at is None:
                raise ContractViolation(
                    ErrorCode.INVALID_INPUT, "retry transition needs available_at"
                )
        elif available_at is not None:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "available_at is only valid for retry")

        updated = connection.execute(
            "UPDATE aftercare_runs SET state=%s, lease_owner=NULL, lease_until=NULL, "
            "wait_id=%s, wait_generation=%s, available_at=%s WHERE tenant_id=%s AND case_id=%s "
            "AND run_id=%s RETURNING " + ",".join(_RUN_FIELDS),
            (
                target,
                wait_id if target in ("WAITING_INPUT", "WAITING_APPROVAL") else None,
                wait_generation if target in ("WAITING_INPUT", "WAITING_APPROVAL") else None,
                available_at if target == "RETRY_AT" else None,
                claim.tenant_id,
                claim.case_id,
                claim.run_id,
            ),
        ).fetchone()
        assert updated is not None
        return _run(updated)


_SESSION_FIELDS = ("tenant_id", "case_id", "session_id", "channel")


class SessionRepository:
    """Scoped persistence for durable sessions (not transport connections)."""

    def create(self, connection: psycopg.Connection[Any], session: SessionRecord) -> None:
        connection.execute(
            "INSERT INTO aftercare_sessions(tenant_id,case_id,session_id,channel) "
            "VALUES (%s,%s,%s,%s)",
            (session.tenant_id, session.case_id, session.session_id, session.channel),
        )

    def get(
        self, connection: psycopg.Connection[Any], tenant: str, case_id: str, session_id: str
    ) -> SessionRecord | None:
        row = connection.execute(
            "SELECT "
            + ",".join(_SESSION_FIELDS)
            + " FROM aftercare_sessions WHERE tenant_id=%s AND case_id=%s AND session_id=%s",
            (tenant, case_id, session_id),
        ).fetchone()
        return (
            None
            if row is None
            else SessionRecord.model_validate(dict(zip(_SESSION_FIELDS, row, strict=False)))
        )


_STEP_FIELDS = ("tenant_id", "case_id", "run_id", "step_id", "kind", "input_version")


class StepRepository:
    """Scoped persistence for logical steps; retries retain the step id."""

    def create(self, connection: psycopg.Connection[Any], step: StepRecord) -> None:
        connection.execute(
            "INSERT INTO aftercare_steps(tenant_id,case_id,run_id,step_id,kind,input_version) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                step.tenant_id,
                step.case_id,
                step.run_id,
                step.step_id,
                step.kind,
                step.input_version,
            ),
        )

    def get(
        self,
        connection: psycopg.Connection[Any],
        tenant: str,
        case_id: str,
        run_id: str,
        step_id: str,
    ) -> StepRecord | None:
        row = connection.execute(
            "SELECT "
            + ",".join(_STEP_FIELDS)
            + " FROM aftercare_steps WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "AND step_id=%s",
            (tenant, case_id, run_id, step_id),
        ).fetchone()
        return (
            None
            if row is None
            else StepRecord.model_validate(dict(zip(_STEP_FIELDS, row, strict=False)))
        )


_ATTEMPT_FIELDS = (
    "tenant_id",
    "case_id",
    "run_id",
    "step_id",
    "attempt_id",
    "attempt_number",
    "fencing_token",
    "status",
)


class AttemptRepository:
    """Append-only-ish records of actual step attempts and their fence."""

    def create(self, connection: psycopg.Connection[Any], attempt: AttemptRecord) -> None:
        connection.execute(
            "INSERT INTO aftercare_attempts(tenant_id,case_id,run_id,step_id,attempt_id,"
            "attempt_number,fencing_token,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                attempt.tenant_id,
                attempt.case_id,
                attempt.run_id,
                attempt.step_id,
                attempt.attempt_id,
                attempt.attempt_number,
                attempt.fencing_token,
                attempt.status,
            ),
        )

    def get(
        self,
        connection: psycopg.Connection[Any],
        tenant: str,
        case_id: str,
        run_id: str,
        attempt_id: str,
    ) -> AttemptRecord | None:
        row = connection.execute(
            "SELECT "
            + ",".join(_ATTEMPT_FIELDS)
            + " FROM aftercare_attempts WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "AND attempt_id=%s",
            (tenant, case_id, run_id, attempt_id),
        ).fetchone()
        return (
            None
            if row is None
            else AttemptRecord.model_validate(dict(zip(_ATTEMPT_FIELDS, row, strict=False)))
        )

    def set_status(
        self,
        connection: psycopg.Connection[Any],
        attempt: AttemptRecord,
        status: str,
        claim: ExecutionClaim,
    ) -> AttemptRecord:
        """Finish an attempt only while its originating execution claim is valid."""
        if (attempt.tenant_id, attempt.case_id, attempt.run_id) != (
            claim.tenant_id,
            claim.case_id,
            claim.run_id,
        ) or attempt.fencing_token != claim.fencing_token:
            raise ContractViolation(ErrorCode.LEASE_LOST, "attempt fence is stale")
        if status not in ("SUCCEEDED", "FAILED", "UNKNOWN"):
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid attempt status")
        run = connection.execute(
            "SELECT state,lease_owner,fencing_token,lease_until FROM aftercare_runs "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s FOR UPDATE",
            (claim.tenant_id, claim.case_id, claim.run_id),
        ).fetchone()
        if (
            run is None
            or run[0] != "RUNNING"
            or run[1] != claim.owner
            or run[2] != claim.fencing_token
        ):
            raise ContractViolation(ErrorCode.LEASE_LOST, "execution lease lost")
        valid = connection.execute(
            "SELECT 1 FROM aftercare_runs WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "AND lease_until > clock_timestamp()",
            (claim.tenant_id, claim.case_id, claim.run_id),
        ).fetchone()
        if valid is None:
            raise ContractViolation(ErrorCode.LEASE_LOST, "execution lease lost")
        row = connection.execute(
            "SELECT "
            + ",".join(_ATTEMPT_FIELDS)
            + " FROM aftercare_attempts WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "AND step_id=%s AND attempt_id=%s FOR UPDATE",
            (
                attempt.tenant_id,
                attempt.case_id,
                attempt.run_id,
                attempt.step_id,
                attempt.attempt_id,
            ),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "attempt not found")
        current = AttemptRecord.model_validate(dict(zip(_ATTEMPT_FIELDS, row, strict=False)))
        if current.status != "STARTED":
            raise ContractViolation(ErrorCode.CONFLICT, "attempt is already terminal")
        updated = connection.execute(
            "UPDATE aftercare_attempts SET status=%s WHERE tenant_id=%s AND case_id=%s "
            "AND run_id=%s "
            "AND step_id=%s AND attempt_id=%s RETURNING " + ",".join(_ATTEMPT_FIELDS),
            (
                status,
                attempt.tenant_id,
                attempt.case_id,
                attempt.run_id,
                attempt.step_id,
                attempt.attempt_id,
            ),
        ).fetchone()
        assert updated is not None
        return AttemptRecord.model_validate(dict(zip(_ATTEMPT_FIELDS, updated, strict=False)))


class CheckpointRepository:
    def save(
        self,
        connection: psycopg.Connection[Any],
        checkpoint: Checkpoint,
        claim: ExecutionClaim,
    ) -> None:
        row = connection.execute(
            "SELECT state, lease_owner, fencing_token, lease_until "
            "FROM aftercare_runs WHERE tenant_id=%s AND case_id=%s AND run_id=%s FOR UPDATE",
            (claim.tenant_id, claim.case_id, claim.run_id),
        ).fetchone()
        if row is None or (
            row[0] != "RUNNING"
            or row[1] != claim.owner
            or row[2] != claim.fencing_token
            or row[3] is None
        ):
            raise ContractViolation(ErrorCode.LEASE_LOST, "execution lease lost")
        valid = connection.execute(
            "SELECT 1 FROM aftercare_runs WHERE tenant_id=%s AND run_id=%s "
            "AND lease_until > clock_timestamp()",
            (claim.tenant_id, claim.run_id),
        ).fetchone()
        if valid is None or (checkpoint.tenant_id, checkpoint.case_id, checkpoint.run_id) != (
            claim.tenant_id,
            claim.case_id,
            claim.run_id,
        ):
            raise ContractViolation(ErrorCode.LEASE_LOST, "execution lease lost")
        if checkpoint.saved_fencing_token != claim.fencing_token:
            raise ContractViolation(ErrorCode.LEASE_LOST, "checkpoint fence is stale")
        connection.execute(
            "INSERT INTO aftercare_checkpoints(tenant_id,case_id,run_id,checkpoint_version,"
            "saved_fencing_token,payload) VALUES (%s,%s,%s,%s,%s,%s)",
            (
                checkpoint.tenant_id,
                checkpoint.case_id,
                checkpoint.run_id,
                checkpoint.checkpoint_version,
                checkpoint.saved_fencing_token,
                Jsonb(checkpoint.model_dump(mode="json")),
            ),
        )

    def get_latest(
        self, connection: psycopg.Connection[Any], tenant: str, run_id: str
    ) -> Checkpoint | None:
        row = connection.execute(
            "SELECT payload FROM aftercare_checkpoints WHERE tenant_id=%s AND run_id=%s "
            "ORDER BY checkpoint_version DESC LIMIT 1",
            (tenant, run_id),
        ).fetchone()
        return (
            None
            if row is None
            else Checkpoint.model_validate_json(json.dumps(row[0], ensure_ascii=False))
        )
