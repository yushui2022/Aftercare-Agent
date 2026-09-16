"""Turn a trusted connector answer into auditable investigation evidence.

The commerce connector answers from an imported export and reports the source
identity the deployment gave it.  This module is the only place that turns one
of those answers into the evidence contract the deterministic assessment and
the EGM adapter already speak.  It is pure: no clock of its own, no database,
no model input, and no new query -- everything it needs is passed in, so the
same answer reconstructs the same evidence byte for byte.  That equality is
what the observation ledger's dedupe compares, so a fact that arrives twice is
recorded once instead of being reported as a conflict.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256

from aftercare_agent.connectors.commerce import (
    ORDER_FACTS_SCHEMA,
    TRACKING_FACTS_SCHEMA,
    CommerceConnector,
    ConnectorAnswer,
)
from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope, utc
from aftercare_agent.domain.investigation import (
    DeliveryStatus,
    InvestigationEvidence,
    InvestigationScope,
    LogisticsObservation,
    OrderSnapshot,
    OriginalReference,
    SourceGrant,
    SourceKind,
)
from aftercare_agent.domain.protocol import ArtifactReference, ToolRequest
from aftercare_agent.persistence import Database

from .egm import InvestigationEvidenceAdapter

# Which connector answer becomes which evidence type.  An answer whose schema is
# not here is not a business fact anyone may cite, so the bridge refuses it
# rather than storing a shape the assessment cannot read.
SCHEMA_KINDS: Mapping[str, SourceKind] = {
    ORDER_FACTS_SCHEMA: SourceKind.ORDER_LEDGER,
    TRACKING_FACTS_SCHEMA: SourceKind.CARRIER,
}
TOOL_SCHEMAS: Mapping[str, str] = {
    "lookup_order": ORDER_FACTS_SCHEMA,
    "lookup_tracking": TRACKING_FACTS_SCHEMA,
}
MAX_EXCERPT = 512


def registered_source_kinds(connector: CommerceConnector) -> Mapping[str, SourceKind]:
    """Map every source the connector speaks for to the kind its answers can be."""
    kinds: dict[str, SourceKind] = {}
    for tool, source_id in connector.registered_sources().items():
        schema = TOOL_SCHEMAS.get(tool)
        if schema is None:
            raise ContractViolation(ErrorCode.CONFLICT, "connector tool has no evidence schema")
        kinds[source_id] = SCHEMA_KINDS[schema]
    return kinds


def _section(body: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = body.get(key)
    return value if isinstance(value, Mapping) else {}


def _excerpt(schema: str, body: Mapping[str, object]) -> str:
    """A bounded, canonical extract of the facts, never free-form prose."""
    if schema == ORDER_FACTS_SCHEMA:
        payment = _section(body, "payment")
        facts: dict[str, object] = {
            "order_id": body.get("order_id"),
            "status": body.get("status"),
            "buyer_id": body.get("buyer_id"),
            "payment_status": payment.get("status"),
            "payment_amount": payment.get("amount"),
        }
    else:
        shipment = _section(body, "shipment")
        facts = {
            "order_id": body.get("order_id"),
            "carrier": body.get("carrier"),
            "tracking_number": body.get("tracking_number"),
            "delivered_at": shipment.get("delivered_at"),
            "latest_event": shipment.get("latest_event"),
        }
    rendered = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return rendered[:MAX_EXCERPT]


def _delivery_status(body: Mapping[str, object]) -> DeliveryStatus:
    """Read the carrier verdict out of the answer, never out of a claim."""
    shipment = _section(body, "shipment")
    if not shipment:
        # No dispatch is a fact about this order, not a delivery verdict.
        return DeliveryStatus.UNKNOWN
    if shipment.get("delivered_at") is not None:
        return DeliveryStatus.DELIVERED
    return DeliveryStatus.IN_TRANSIT


def evidence_from_answer(
    answer: ConnectorAnswer,
    *,
    scope: InvestigationScope,
    artifact: ArtifactReference,
    registered_sources: Mapping[str, SourceKind],
) -> InvestigationEvidence:
    """Convert one stored connector answer into case-scoped, citable evidence.

    Every check here is a refusal, not a repair: an answer about another order,
    an artifact whose bytes are not the ones the connector produced, a source
    that is not registered for this kind, or a schema nobody can assess.  What
    comes out is therefore still the connector's own fact, with the digest of
    the bytes that answered it.
    """
    if (artifact.tenant_id, artifact.case_id) != (scope.tenant_id, scope.case_id):
        raise ContractViolation(ErrorCode.FORBIDDEN, "artifact does not belong to this case")
    if sha256(answer.content()).hexdigest() != artifact.sha256:
        raise ContractViolation(ErrorCode.CONFLICT, "stored answer is not the connector's bytes")
    raw_schema = answer.body.get("schema")
    schema = raw_schema if isinstance(raw_schema, str) else ""
    kind = SCHEMA_KINDS.get(schema)
    if kind is None:
        raise ContractViolation(ErrorCode.EVIDENCE_REJECTED, "answer schema is not evidence")
    if registered_sources.get(answer.source_id) != kind:
        raise ContractViolation(ErrorCode.FORBIDDEN, "source has no registered capability")
    if answer.body.get("order_id") != scope.order_id:
        raise ContractViolation(ErrorCode.FORBIDDEN, "answer is about another order")
    observed_at = utc(answer.observed_at)
    # Addressed by digest rather than by the per-call label, which changes
    # whenever the same fact is fetched again.
    original = OriginalReference(
        artifact_id=artifact.sha256,
        sha256=artifact.sha256,
        excerpt=_excerpt(schema, answer.body),
    )
    # Derived from the trusted source event, so the same fact re-delivered keeps
    # one identity and a different fact cannot borrow it.
    evidence_id = f"{answer.source_id}:{answer.source_event_id}"
    if kind is SourceKind.ORDER_LEDGER:
        return OrderSnapshot(
            tenant_id=scope.tenant_id,
            case_id=scope.case_id,
            order_id=scope.order_id,
            evidence_id=evidence_id,
            source_id=answer.source_id,
            source_event_id=answer.source_event_id,
            observed_at=observed_at,
            # The export is the receipt: an import the deployment performs is
            # the moment Aftercare could first know this fact, and a wall clock
            # here would make every replay of one unchanged fact a conflict.
            received_at=observed_at,
            original=original,
        )
    return LogisticsObservation(
        tenant_id=scope.tenant_id,
        case_id=scope.case_id,
        order_id=scope.order_id,
        evidence_id=evidence_id,
        source_id=answer.source_id,
        source_event_id=answer.source_event_id,
        observed_at=observed_at,
        received_at=observed_at,
        original=original,
        delivery_status=_delivery_status(answer.body),
    )


class ConnectorEvidenceRecorder:
    """Persist each evidence-bearing answer in the durable observation ledger.

    The executor calls this while the sandbox lease is still held and the
    artifact is already stored, so a fact is either recorded with the bytes
    that produced it or the step fails.  Recording is idempotent by
    ``(source_id, source_event_id)``: a replayed step re-derives the same
    evidence and the ledger returns the row it already has.
    """

    def __init__(
        self,
        *,
        database: Database,
        adapter: InvestigationEvidenceAdapter,
        grants: Mapping[str, SourceGrant],
    ) -> None:
        scope = adapter.scope
        if not grants:
            raise ValueError("at least one source grant is required")
        for source_id, grant in grants.items():
            if source_id != grant.source_id:
                raise ValueError("source grant is filed under another source")
            if (grant.tenant_id, grant.case_id, grant.order_id) != (
                scope.tenant_id,
                scope.case_id,
                scope.order_id,
            ):
                raise ValueError("source grant does not belong to the investigation scope")
        self._database = database
        self._adapter = adapter
        self._grants = dict(grants)
        self._scope = scope

    def __call__(
        self,
        *,
        request: ToolRequest,
        scope: RunScope,
        answer: ConnectorAnswer,
        artifact: ArtifactReference,
    ) -> InvestigationEvidence:
        del request
        if (scope.tenant_id, scope.case_id) != (self._scope.tenant_id, self._scope.case_id):
            raise ContractViolation(ErrorCode.FORBIDDEN, "answer belongs to another case")
        grant = self._grants.get(answer.source_id)
        if grant is None:
            # A connector that speaks for several sources holds one grant per
            # source; an answer from an ungranted source is not recorded.
            raise ContractViolation(ErrorCode.FORBIDDEN, "answer source is not granted")
        evidence = evidence_from_answer(
            answer,
            scope=self._scope,
            artifact=artifact,
            registered_sources=self._adapter.registered_sources,
        )
        # Wall clock is used only to refuse evidence dated in the future; it is
        # never persisted, so it cannot make two recordings of one fact differ.
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            return self._adapter.persist_observation(connection, evidence, grant, now=now)


__all__ = [
    "ConnectorEvidenceRecorder",
    "MAX_EXCERPT",
    "SCHEMA_KINDS",
    "TOOL_SCHEMAS",
    "evidence_from_answer",
    "registered_source_kinds",
]
