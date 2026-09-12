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
from typing import Protocol

from evidence_gated_memory.application import EvidenceApplication, Principal

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


_EVIDENCE_TYPES = {
    SourceKind.ORDER_LEDGER.value: "order_record",
    SourceKind.CARRIER.value: "logistics_observation",
    SourceKind.BUYER_CHANNEL.value: "buyer_statement",
}


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
        return self.application.ingest(self.connector, self.scope.case_id, command)

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
