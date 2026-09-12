"""Atomic case admission and idempotency primitives."""

from dataclasses import dataclass
from typing import Any

import psycopg

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import (
    AdmissionKey,
    CaseRecord,
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
