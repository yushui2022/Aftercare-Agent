"""Constrained investigation-to-EGM bridge.

Connector observations are validated before entering EGM.  The model-facing
surface only accepts the already validated :class:`InvestigationProposal`; it
cannot choose a tenant, order, source, timestamp or free-form claim text.
Assessment remains deterministic and is computed over the complete authorized
observation set supplied by the trusted host.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

import psycopg
from evidence_gated_memory.application import EvidenceApplication, Principal
from evidence_gated_memory.schemas.loader import DomainSchema, load_schema

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import (
    FreshnessPolicy,
    InvestigationAssessment,
    InvestigationEvidence,
    InvestigationProposal,
    InvestigationScope,
    SourceGrant,
    SourceKind,
    assess_investigation,
    ingest_observation,
    source_kind,
)
from aftercare_agent.persistence.investigations import InvestigationObservationRepository


class _Application(Protocol):
    def ingest(
        self, principal: Principal, case_id: str, command: Mapping[str, object]
    ) -> object: ...

    def context(
        self,
        principal: Principal,
        case_id: str,
        *,
        query: str | None = None,
        include_long_term: bool = False,
        max_facts: int = 10,
    ) -> object: ...


class UnboundEvidenceApplication:
    """Fail closed when a host has not supplied an EGM application binding.

    The normal model Worker supplies an embedded EGM application.  Local
    callers may intentionally build a recorder without one; this fallback
    keeps the observation ledger path explicit and refuses any operation that
    would otherwise look like a successful EGM write.
    """

    def ingest(self, principal: Principal, case_id: str, command: Mapping[str, object]) -> object:
        del principal, case_id, command
        raise ContractViolation(ErrorCode.CONFLICT, "EGM ingest is not wired for this Worker")

    def context(
        self,
        principal: Principal,
        case_id: str,
        *,
        query: str | None = None,
        include_long_term: bool = False,
        max_facts: int = 10,
    ) -> object:
        del principal, case_id, query, include_long_term, max_facts
        raise ContractViolation(ErrorCode.CONFLICT, "EGM context is not wired for this Worker")


_EVIDENCE_TYPES = {
    SourceKind.ORDER_LEDGER.value: "order_record",
    SourceKind.CARRIER.value: "logistics_observation",
    SourceKind.BUYER_CHANNEL.value: "buyer_statement",
}

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "investigation.yaml"

_SCHEMA_SOURCE_KINDS = {
    "order_record": SourceKind.ORDER_LEDGER.value,
    "logistics_observation": SourceKind.CARRIER.value,
    "buyer_statement": SourceKind.BUYER_CHANNEL.value,
}


def investigation_schema(registered_sources: Mapping[str, SourceKind]) -> DomainSchema:
    """Load the investigation contract and bind deployment source IDs to it."""
    base = load_schema(_SCHEMA_PATH)
    by_kind: dict[str, list[str]] = {}
    for source_id, kind in registered_sources.items():
        by_kind.setdefault(kind.value, []).append(source_id)
    evidence_types = [
        evidence_type.model_copy(
            update={"source_systems": by_kind.get(_SCHEMA_SOURCE_KINDS[evidence_type.name], [])}
        )
        for evidence_type in base.evidence_types
    ]
    if any(not item.source_systems for item in evidence_types):
        raise ValueError("every investigation evidence type needs a registered source")
    return base.model_copy(update={"evidence_types": evidence_types})


def build_investigation_application(
    provider: object, registered_sources: Mapping[str, SourceKind]
) -> EvidenceApplication:
    """Build an EGM application with the Aftercare investigation schema."""
    return EvidenceApplication(  # type: ignore[no-untyped-call]
        provider, domain_schema=investigation_schema(registered_sources)
    )


class InvestigationEvidenceAdapter:
    """Worker-side adapter; EGM remains an evidence store, not business authority."""

    def __init__(
        self,
        application: EvidenceApplication | _Application,
        scope: InvestigationScope,
        *,
        connector: Principal,
        model_tools: Principal,
        registered_sources: Mapping[str, SourceKind],
        egm_writer: Principal | None = None,
    ) -> None:
        if connector.tenant_id != scope.tenant_id or model_tools.tenant_id != scope.tenant_id:
            raise ValueError("principals must belong to the investigation tenant")
        if "evidence:write" not in connector.permissions:
            raise ValueError("connector must have evidence:write")
        if not model_tools.permissions <= {"context:read"}:
            raise ValueError("model tools may only read context")
        self.application = application
        self.scope = scope
        self.connector = connector
        self.model_tools = model_tools
        self.registered_sources = dict(registered_sources)
        if egm_writer is not None:
            if egm_writer.tenant_id != scope.tenant_id:
                raise ValueError("EGM writer must belong to the investigation tenant")
            if "task:write" not in egm_writer.permissions:
                raise ValueError("EGM writer must have task:write")
            if callable(getattr(application, "revoke_evidence", None)) and (
                "evidence:revoke" not in egm_writer.permissions
            ):
                raise ValueError("EGM writer must have evidence:revoke")
            self.egm_writer = egm_writer
        else:
            self.egm_writer = connector

    def persist_observation(
        self,
        connection: psycopg.Connection[object],
        evidence: InvestigationEvidence,
        grant: SourceGrant,
        *,
        now: datetime,
    ) -> InvestigationEvidence:
        """Validate and persist one trusted connector observation."""
        accepted = ingest_observation(
            self.scope,
            grant,
            self.registered_sources,
            evidence,
            now=now,
        )
        return InvestigationObservationRepository().put(connection, accepted, scope=self.scope)

    def persisted_observations(
        self, connection: psycopg.Connection[object]
    ) -> tuple[InvestigationEvidence, ...]:
        """Reload the complete authorized ledger for deterministic assessment."""
        return InvestigationObservationRepository().list_case(connection, scope=self.scope)

    def assess_persisted(
        self,
        connection: psycopg.Connection[object],
        proposal: InvestigationProposal,
        policy: FreshnessPolicy,
        *,
        now: datetime,
    ) -> InvestigationAssessment:
        repository = InvestigationObservationRepository()
        repository.lock_case(connection, scope=self.scope)
        return self.assess(
            proposal,
            repository.list_case(connection, scope=self.scope),
            policy,
            now=now,
        )

    def ingest(
        self,
        evidence: InvestigationEvidence,
        grant: SourceGrant,
        *,
        node_id: str,
        operation_id: str,
        expected_revision: int,
        now: datetime,
    ) -> object:
        """Validate trusted connector input, then persist canonical evidence in EGM."""
        if grant.subject_id != self.connector.subject:
            raise ValueError("source grant subject does not match connector")
        accepted = ingest_observation(
            self.scope,
            grant,
            self.registered_sources,
            evidence,
            now=now,
        )
        kind = source_kind(accepted)
        evidence_type = _EVIDENCE_TYPES[kind.value]
        command = {
            "operation_id": operation_id,
            "expected_revision": expected_revision,
            "node_id": node_id,
            "evidence_type": evidence_type,
            # The custom EGM schema must allow these source-system names.  It
            # is intentionally derived from the trusted registry, never model input.
            "source_system": accepted.source_id,
            "observed_at": accepted.observed_at,
            "content": json.dumps(
                accepted.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
            ),
            "summary": f"trusted {kind.value} observation",
        }
        return self.application.ingest(self.egm_writer, self.scope.case_id, command)

    def revoke(
        self,
        *,
        egm_evidence_id: str,
        operation_id: str,
        expected_revision: int,
        reason: str,
    ) -> object:
        """Submit one host-authorized, idempotent correction to EGM."""
        revoke_evidence = getattr(self.application, "revoke_evidence", None)
        if not callable(revoke_evidence):
            raise ContractViolation(ErrorCode.CONFLICT, "EGM revocation is not available")
        return revoke_evidence(
            self.egm_writer,
            self.scope.case_id,
            egm_evidence_id,
            {
                "operation_id": operation_id,
                "expected_revision": expected_revision,
                "reason": reason,
            },
        )

    def assess(
        self,
        proposal: InvestigationProposal,
        observations: Sequence[InvestigationEvidence],
        policy: FreshnessPolicy,
        *,
        now: datetime,
    ) -> InvestigationAssessment:
        """Evaluate the full host-authorized ledger; no model text is persisted."""
        return assess_investigation(
            self.scope,
            proposal,
            observations,
            self.registered_sources,
            policy,
            now=now,
        )

    def context(self, *, query: str | None = None, max_facts: int = 10) -> object:
        """Read case-scoped EGM context without promoting long-term memory."""
        return self.application.context(
            self.model_tools,
            self.scope.case_id,
            query=query,
            include_long_term=False,
            max_facts=max_facts,
        )
