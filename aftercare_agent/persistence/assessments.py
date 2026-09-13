"""PostgreSQL persistence for immutable investigation result snapshots."""

import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import InvestigationAssessment
from aftercare_agent.domain.recommendations import (
    InvestigationAssessmentRecord,
    assessment_digest,
)

_FIELDS = (
    "tenant_id",
    "case_id",
    "run_id",
    "assessment_id",
    "assessment_sha256",
    "policy_id",
    "policy_version",
    "disposition",
    "payload",
    "created_at",
)


def _record(row: tuple[Any, ...]) -> InvestigationAssessmentRecord:
    data = dict(zip(_FIELDS, row, strict=False))
    data["assessment"] = InvestigationAssessment.model_validate_json(
        json.dumps(data.pop("payload"), ensure_ascii=False)
    )
    return InvestigationAssessmentRecord.model_validate(data)


class InvestigationAssessmentRepository:
    """Append-only snapshots with exact digest replay protection."""

    def put(
        self,
        connection: psycopg.Connection[Any],
        *,
        tenant_id: str,
        case_id: str,
        run_id: str,
        assessment: InvestigationAssessment,
    ) -> InvestigationAssessmentRecord:
        if (assessment.tenant_id, assessment.case_id) != (tenant_id, case_id):
            raise ContractViolation(ErrorCode.FORBIDDEN, "assessment scope mismatch")
        digest = assessment_digest(assessment)
        record_id = f"assessment:{digest}"
        inserted = connection.execute(
            "INSERT INTO aftercare_investigation_assessments(tenant_id,case_id,run_id,"
            "assessment_id,assessment_sha256,policy_id,policy_version,disposition,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING "
            + ",".join(_FIELDS),
            (
                tenant_id,
                case_id,
                run_id,
                record_id,
                digest,
                assessment.policy_id,
                assessment.policy_version,
                assessment.disposition.value,
                Jsonb(assessment.model_dump(mode="json")),
            ),
        ).fetchone()
        if inserted is not None:
            return _record(inserted)
        row = connection.execute(
            "SELECT " + ",".join(_FIELDS) + " FROM aftercare_investigation_assessments "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s AND assessment_id=%s FOR UPDATE",
            (tenant_id, case_id, run_id, record_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "assessment outcome is unknown")
        current = _record(row)
        if current.assessment_sha256 != digest or current.assessment != assessment:
            raise ContractViolation(ErrorCode.CONFLICT, "assessment replay changed")
        return current

    def get_latest(
        self, connection: psycopg.Connection[Any], *, tenant_id: str, case_id: str, run_id: str
    ) -> InvestigationAssessmentRecord | None:
        row = connection.execute(
            "SELECT " + ",".join(_FIELDS) + " FROM aftercare_investigation_assessments "
            "WHERE tenant_id=%s AND case_id=%s AND run_id=%s "
            "ORDER BY created_at DESC,assessment_id DESC LIMIT 1",
            (tenant_id, case_id, run_id),
        ).fetchone()
        return None if row is None else _record(row)


__all__ = ["InvestigationAssessmentRepository"]
