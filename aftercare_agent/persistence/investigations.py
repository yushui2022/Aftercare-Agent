"""Durable, case-scoped investigation observation ledger."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter, ValidationError

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import (
    InvestigationEvidence,
    InvestigationScope,
)
from aftercare_agent.persistence.admission import CaseRepository

_EVIDENCE: TypeAdapter[InvestigationEvidence] = TypeAdapter(InvestigationEvidence)


@dataclass(frozen=True)
class InvestigationEgmBinding:
    """Aftercare's durable pointer to the EGM investigation node."""

    tenant_id: str
    case_id: str
    order_id: str
    node_id: str
    schema_fingerprint: str


@dataclass(frozen=True)
class InvestigationEgmProjection:
    """Durable command and completion receipt for one EGM projection."""

    tenant_id: str
    case_id: str
    order_id: str
    evidence_id: str
    operation_id: str
    expected_revision: int
    completed_revision: int | None
    egm_evidence_id: str | None


@dataclass(frozen=True)
class InvestigationEgmRevocation:
    """Durable source correction and its exact EGM command receipt."""

    tenant_id: str
    case_id: str
    order_id: str
    evidence_id: str
    egm_evidence_id: str
    operation_id: str
    reason: str
    revoked_at: datetime
    expected_revision: int
    completed_revision: int | None
    egm_revoked_at: datetime | None


@dataclass(frozen=True)
class BuyerHistoryCursor:
    """Last buyer message fully written for one Case and source."""

    tenant_id: str
    case_id: str
    order_id: str
    source_id: str
    message_id: str


def _decode(payload: object) -> InvestigationEvidence:
    try:
        return _EVIDENCE.validate_json(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(
            ErrorCode.RETRYABLE, "stored investigation observation is invalid"
        ) from exc


class InvestigationObservationRepository:
    """Persist and reload observations without trusting model-selected fields."""

    def lock_case(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
    ) -> None:
        """Serialize ledger mutation and final assessment for one Case."""
        order_id = CaseRepository().lock_order_id(connection, scope.tenant_id, scope.case_id)
        if order_id != scope.order_id:
            raise ContractViolation(ErrorCode.CONFLICT, "investigation order binding changed")

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
        self.lock_case(connection, scope=scope)
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
        reason: str,
    ) -> InvestigationEvidence:
        """Record a trusted revocation while retaining the original payload."""
        if not reason.strip() or len(reason) > 4000:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid revocation reason")
        self.lock_case(connection, scope=scope)
        row = connection.execute(
            "SELECT payload,revoked_at,revocation_reason FROM aftercare_investigation_observations "
            "WHERE tenant_id=%s AND case_id=%s AND order_id=%s AND evidence_id=%s FOR UPDATE",
            (scope.tenant_id, scope.case_id, scope.order_id, evidence_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "observation not found")
        existing = _decode(row[0])
        if existing.revoked_at is not None and (
            existing.revoked_at != revoked_at or row[2] != reason
        ):
            raise ContractViolation(ErrorCode.CONFLICT, "observation revocation conflicts")
        try:
            updated = _EVIDENCE.validate_python(
                {**existing.model_dump(mode="python"), "revoked_at": revoked_at}
            )
        except ValidationError as exc:
            raise ContractViolation(
                ErrorCode.EVIDENCE_REJECTED, "observation revocation timestamp is invalid"
            ) from exc
        connection.execute(
            "UPDATE aftercare_investigation_observations "
            "SET revoked_at=%s,revocation_reason=%s,payload=%s "
            "WHERE tenant_id=%s AND case_id=%s AND order_id=%s AND evidence_id=%s",
            (
                revoked_at,
                reason,
                Jsonb(updated.model_dump(mode="json")),
                scope.tenant_id,
                scope.case_id,
                scope.order_id,
                evidence_id,
            ),
        )
        return updated


class InvestigationEgmBindingRepository:
    """Persist node identity without pretending to own EGM's revision."""

    def get(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
    ) -> InvestigationEgmBinding | None:
        row = connection.execute(
            "SELECT tenant_id,case_id,order_id,node_id,schema_fingerprint "
            "FROM aftercare_investigation_egm_bindings "
            "WHERE tenant_id=%s AND case_id=%s",
            (scope.tenant_id, scope.case_id),
        ).fetchone()
        if row is None:
            return None
        binding = InvestigationEgmBinding(*row)
        if binding.order_id != scope.order_id:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM node binding order conflicts")
        return binding

    def put(
        self,
        connection: psycopg.Connection[Any],
        binding: InvestigationEgmBinding,
    ) -> InvestigationEgmBinding:
        connection.execute(
            "INSERT INTO aftercare_investigation_egm_bindings("
            "tenant_id,case_id,order_id,node_id,schema_fingerprint) VALUES "
            "(%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,case_id) DO NOTHING",
            (
                binding.tenant_id,
                binding.case_id,
                binding.order_id,
                binding.node_id,
                binding.schema_fingerprint,
            ),
        )
        existing = self.get(
            connection,
            scope=InvestigationScope(
                tenant_id=binding.tenant_id,
                case_id=binding.case_id,
                order_id=binding.order_id,
            ),
        )
        if existing is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "EGM binding write outcome is unknown")
        if existing != binding:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM node binding conflicts")
        return existing


class _InvestigationEgmProjectionRepositoryBase:
    """Keep an EGM command stable across retries and crash recovery."""

    def get(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        evidence_id: str,
    ) -> InvestigationEgmProjection | None:
        row = connection.execute(
            "SELECT tenant_id,case_id,order_id,evidence_id,operation_id,expected_revision,"
            "completed_revision,egm_evidence_id "
            "FROM aftercare_investigation_egm_projections "
            "WHERE tenant_id=%s AND case_id=%s AND evidence_id=%s",
            (scope.tenant_id, scope.case_id, evidence_id),
        ).fetchone()
        if row is None:
            return None
        projection = InvestigationEgmProjection(*row)
        if projection.order_id != scope.order_id:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM projection order conflicts")
        return projection


class InvestigationEgmRevocationRepository:
    """Keep an EGM revocation command stable across retries and crashes."""

    def get(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        evidence_id: str,
    ) -> InvestigationEgmRevocation | None:
        row = connection.execute(
            "SELECT tenant_id,case_id,order_id,evidence_id,egm_evidence_id,operation_id,"
            "reason,revoked_at,expected_revision,completed_revision,egm_revoked_at "
            "FROM aftercare_investigation_egm_revocations "
            "WHERE tenant_id=%s AND case_id=%s AND evidence_id=%s",
            (scope.tenant_id, scope.case_id, evidence_id),
        ).fetchone()
        if row is None:
            return None
        revocation = InvestigationEgmRevocation(*row)
        if revocation.order_id != scope.order_id:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM revocation order conflicts")
        return revocation

    def reserve(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        evidence_id: str,
        egm_evidence_id: str,
        operation_id: str,
        reason: str,
        revoked_at: datetime,
        expected_revision: int,
    ) -> InvestigationEgmRevocation:
        if (
            not evidence_id
            or not egm_evidence_id
            or not operation_id
            or not reason.strip()
            or len(reason) > 4000
            or type(expected_revision) is not int
            or expected_revision < 0
        ):
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid EGM revocation reservation")
        connection.execute(
            "INSERT INTO aftercare_investigation_egm_revocations("
            "tenant_id,case_id,order_id,evidence_id,egm_evidence_id,operation_id,reason,"
            "revoked_at,expected_revision) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tenant_id,case_id,evidence_id) DO NOTHING",
            (
                scope.tenant_id,
                scope.case_id,
                scope.order_id,
                evidence_id,
                egm_evidence_id,
                operation_id,
                reason,
                revoked_at,
                expected_revision,
            ),
        )
        revocation = self.get(connection, scope=scope, evidence_id=evidence_id)
        if revocation is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "EGM revocation reservation is unknown")
        expected = (egm_evidence_id, operation_id, reason, revoked_at)
        actual = (
            revocation.egm_evidence_id,
            revocation.operation_id,
            revocation.reason,
            revocation.revoked_at,
        )
        if actual != expected:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM revocation reservation conflicts")
        return revocation

    def rebase(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        evidence_id: str,
        operation_id: str,
        previous_revision: int,
        expected_revision: int,
    ) -> InvestigationEgmRevocation:
        if expected_revision == previous_revision:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM revocation revision did not advance")
        connection.execute(
            "UPDATE aftercare_investigation_egm_revocations "
            "SET expected_revision=%s,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND case_id=%s AND order_id=%s AND evidence_id=%s "
            "AND operation_id=%s AND expected_revision=%s AND completed_revision IS NULL",
            (
                expected_revision,
                scope.tenant_id,
                scope.case_id,
                scope.order_id,
                evidence_id,
                operation_id,
                previous_revision,
            ),
        )
        revocation = self.get(connection, scope=scope, evidence_id=evidence_id)
        if revocation is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "EGM revocation rebase is unknown")
        if revocation.completed_revision is not None:
            return revocation
        if (
            revocation.operation_id != operation_id
            or revocation.expected_revision != expected_revision
        ):
            raise ContractViolation(ErrorCode.CONFLICT, "EGM revocation rebased elsewhere")
        return revocation

    def complete(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        evidence_id: str,
        operation_id: str,
        expected_revision: int,
        completed_revision: int,
        egm_revoked_at: datetime,
    ) -> InvestigationEgmRevocation:
        if completed_revision <= expected_revision:
            raise ContractViolation(ErrorCode.CONFLICT, "invalid EGM revocation result")
        connection.execute(
            "UPDATE aftercare_investigation_egm_revocations "
            "SET completed_revision=%s,egm_revoked_at=%s,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND case_id=%s AND order_id=%s AND evidence_id=%s "
            "AND operation_id=%s AND expected_revision=%s AND completed_revision IS NULL",
            (
                completed_revision,
                egm_revoked_at,
                scope.tenant_id,
                scope.case_id,
                scope.order_id,
                evidence_id,
                operation_id,
                expected_revision,
            ),
        )
        revocation = self.get(connection, scope=scope, evidence_id=evidence_id)
        if revocation is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "EGM revocation completion is unknown")
        expected = (operation_id, expected_revision, completed_revision, egm_revoked_at)
        actual = (
            revocation.operation_id,
            revocation.expected_revision,
            revocation.completed_revision,
            revocation.egm_revoked_at,
        )
        if actual != expected:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM revocation completion conflicts")
        return revocation


class InvestigationEgmProjectionRepository(_InvestigationEgmProjectionRepositoryBase):
    """Reserve, rebase and complete one evidence projection."""

    def reserve(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        evidence_id: str,
        operation_id: str,
        expected_revision: int,
    ) -> InvestigationEgmProjection:
        if not evidence_id or not operation_id or type(expected_revision) is not int:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid EGM projection reservation")
        if expected_revision < 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid EGM projection revision")
        connection.execute(
            "INSERT INTO aftercare_investigation_egm_projections("
            "tenant_id,case_id,order_id,evidence_id,operation_id,expected_revision) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,case_id,evidence_id) DO NOTHING",
            (
                scope.tenant_id,
                scope.case_id,
                scope.order_id,
                evidence_id,
                operation_id,
                expected_revision,
            ),
        )
        projection = self.get(connection, scope=scope, evidence_id=evidence_id)
        if projection is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "EGM projection reservation is unknown")
        if projection.operation_id != operation_id:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM projection operation conflicts")
        return projection

    def rebase(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        evidence_id: str,
        operation_id: str,
        previous_revision: int,
        expected_revision: int,
    ) -> InvestigationEgmProjection:
        if expected_revision == previous_revision:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM projection revision did not advance")
        connection.execute(
            "UPDATE aftercare_investigation_egm_projections "
            "SET expected_revision=%s,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND case_id=%s AND order_id=%s AND evidence_id=%s "
            "AND operation_id=%s AND expected_revision=%s AND completed_revision IS NULL",
            (
                expected_revision,
                scope.tenant_id,
                scope.case_id,
                scope.order_id,
                evidence_id,
                operation_id,
                previous_revision,
            ),
        )
        projection = self.get(connection, scope=scope, evidence_id=evidence_id)
        if projection is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "EGM projection rebase is unknown")
        if projection.completed_revision is not None:
            return projection
        if (
            projection.operation_id != operation_id
            or projection.expected_revision != expected_revision
        ):
            raise ContractViolation(ErrorCode.CONFLICT, "EGM projection rebased elsewhere")
        return projection

    def complete(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        evidence_id: str,
        operation_id: str,
        expected_revision: int,
        completed_revision: int,
        egm_evidence_id: str,
    ) -> InvestigationEgmProjection:
        if completed_revision <= expected_revision or not egm_evidence_id:
            raise ContractViolation(ErrorCode.CONFLICT, "invalid EGM projection result")
        connection.execute(
            "UPDATE aftercare_investigation_egm_projections "
            "SET completed_revision=%s,egm_evidence_id=%s,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND case_id=%s AND order_id=%s AND evidence_id=%s "
            "AND operation_id=%s AND expected_revision=%s AND completed_revision IS NULL",
            (
                completed_revision,
                egm_evidence_id,
                scope.tenant_id,
                scope.case_id,
                scope.order_id,
                evidence_id,
                operation_id,
                expected_revision,
            ),
        )
        projection = self.get(connection, scope=scope, evidence_id=evidence_id)
        if projection is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "EGM projection completion is unknown")
        expected = (operation_id, expected_revision, completed_revision, egm_evidence_id)
        actual = (
            projection.operation_id,
            projection.expected_revision,
            projection.completed_revision,
            projection.egm_evidence_id,
        )
        if actual != expected:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM projection completion conflicts")
        return projection


class BuyerHistoryCursorRepository:
    """Advance a buyer-history position only after a complete page succeeds."""

    def get(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        source_id: str,
    ) -> BuyerHistoryCursor | None:
        row = connection.execute(
            "SELECT tenant_id,case_id,order_id,source_id,message_id "
            "FROM aftercare_investigation_buyer_cursors "
            "WHERE tenant_id=%s AND case_id=%s AND source_id=%s",
            (scope.tenant_id, scope.case_id, source_id),
        ).fetchone()
        if row is None:
            return None
        cursor = BuyerHistoryCursor(*row)
        if cursor.order_id != scope.order_id:
            raise ContractViolation(ErrorCode.CONFLICT, "buyer history cursor order conflicts")
        return cursor

    def advance(
        self,
        connection: psycopg.Connection[Any],
        *,
        scope: InvestigationScope,
        source_id: str,
        expected_message_id: str | None,
        next_message_id: str,
    ) -> BuyerHistoryCursor:
        if not source_id or not next_message_id or next_message_id == expected_message_id:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "buyer history cursor cannot advance")
        if expected_message_id is None:
            connection.execute(
                "INSERT INTO aftercare_investigation_buyer_cursors("
                "tenant_id,case_id,order_id,source_id,message_id) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id,case_id,source_id) DO NOTHING",
                (
                    scope.tenant_id,
                    scope.case_id,
                    scope.order_id,
                    source_id,
                    next_message_id,
                ),
            )
        else:
            connection.execute(
                "UPDATE aftercare_investigation_buyer_cursors "
                "SET message_id=%s,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND case_id=%s AND order_id=%s AND source_id=%s "
                "AND message_id=%s",
                (
                    next_message_id,
                    scope.tenant_id,
                    scope.case_id,
                    scope.order_id,
                    source_id,
                    expected_message_id,
                ),
            )
        current = self.get(connection, scope=scope, source_id=source_id)
        if current is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "buyer history cursor outcome is unknown")
        if current.message_id != next_message_id:
            raise ContractViolation(ErrorCode.CONFLICT, "buyer history cursor advanced elsewhere")
        return current
