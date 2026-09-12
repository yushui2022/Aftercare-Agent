"""Durable, case-scoped investigation observation ledger."""

import json
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import (
    InvestigationEvidence,
    InvestigationScope,
)

_EVIDENCE: TypeAdapter[InvestigationEvidence] = TypeAdapter(InvestigationEvidence)


def _decode(payload: object) -> InvestigationEvidence:
    try:
        return _EVIDENCE.validate_json(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(
            ErrorCode.RETRYABLE, "stored investigation observation is invalid"
        ) from exc


class InvestigationObservationRepository:
    """Persist and reload observations without trusting model-selected fields."""

    def put(
        self,
        connection: psycopg.Connection[Any],
        evidence: InvestigationEvidence,
        *,
        scope: InvestigationScope,
    ) -> InvestigationEvidence:
        if (evidence.tenant_id, evidence.case_id, evidence.order_id) != (
            scope.tenant_id,
            scope.case_id,
            scope.order_id,
        ):
            raise ContractViolation(ErrorCode.FORBIDDEN, "observation scope mismatch")
        payload = Jsonb(evidence.model_dump(mode="json"))
        inserted = connection.execute(
            "INSERT INTO aftercare_investigation_observations("
            "tenant_id,case_id,order_id,evidence_id,source_id,source_event_id,kind,"
            "observed_at,received_at,revoked_at,payload) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
            (
                evidence.tenant_id,
                evidence.case_id,
                evidence.order_id,
                evidence.evidence_id,
                evidence.source_id,
                evidence.source_event_id,
                evidence.kind,
                evidence.observed_at,
                evidence.received_at,
                evidence.revoked_at,
                payload,
            ),
        ).fetchone()
        if inserted is not None:
            return evidence
        row = connection.execute(
            "SELECT payload FROM aftercare_investigation_observations "
            "WHERE tenant_id=%s AND case_id=%s AND "
            "((evidence_id=%s) OR (source_id=%s AND source_event_id=%s)) "
            "FOR UPDATE",
            (
                evidence.tenant_id,
                evidence.case_id,
                evidence.evidence_id,
                evidence.source_id,
                evidence.source_event_id,
            ),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "observation replay outcome is unknown")
        existing = _decode(row[0])
        if existing != evidence:
            raise ContractViolation(ErrorCode.CONFLICT, "observation replay conflicts")
        return existing

    def list_case(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
    ) -> tuple[InvestigationEvidence, ...]:
        rows = connection.execute(
            "SELECT payload FROM aftercare_investigation_observations "
            "WHERE tenant_id=%s AND case_id=%s AND order_id=%s "
            "ORDER BY evidence_id",
            (scope.tenant_id, scope.case_id, scope.order_id),
        ).fetchall()
        return tuple(_decode(row[0]) for row in rows)

    def revoke(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        evidence_id: str,
        revoked_at: datetime,
    ) -> InvestigationEvidence:
        """Record a trusted revocation while retaining the original payload."""
        row = connection.execute(
            "SELECT payload,revoked_at FROM aftercare_investigation_observations "
            "WHERE tenant_id=%s AND case_id=%s AND order_id=%s AND evidence_id=%s FOR UPDATE",
            (scope.tenant_id, scope.case_id, scope.order_id, evidence_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "observation not found")
        existing = _decode(row[0])
        if existing.revoked_at is not None and existing.revoked_at != revoked_at:
            raise ContractViolation(ErrorCode.CONFLICT, "observation revocation conflicts")
        updated = existing.model_copy(update={"revoked_at": revoked_at})
        connection.execute(
            "UPDATE aftercare_investigation_observations SET revoked_at=%s,payload=%s "
            "WHERE tenant_id=%s AND case_id=%s AND order_id=%s AND evidence_id=%s",
            (
                revoked_at,
                Jsonb(updated.model_dump(mode="json")),
                scope.tenant_id,
                scope.case_id,
                scope.order_id,
                evidence_id,
            ),
        )
        return updated
