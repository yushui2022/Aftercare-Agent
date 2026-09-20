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
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from evidence_gated_memory.application import ApplicationError, Principal

from aftercare_agent.artifacts import ContentAddressedArtifactStore
from aftercare_agent.connectors.commerce import (
    BUYER_FACTS_SCHEMA,
    MAX_BUYER_MESSAGE_PAGE_SIZE,
    ORDER_FACTS_SCHEMA,
    TRACKING_FACTS_SCHEMA,
    CommerceConnector,
    ConnectorAnswer,
)
from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope, utc
from aftercare_agent.domain.investigation import (
    BuyerAssertion,
    BuyerStatement,
    DeliveryStatus,
    FreshnessPolicy,
    InvestigationAssessment,
    InvestigationEvidence,
    InvestigationProposal,
    InvestigationScope,
    LogisticsObservation,
    OrderSnapshot,
    OriginalReference,
    SourceGrant,
    SourceKind,
)
from aftercare_agent.domain.protocol import ArtifactReference, ToolRequest
from aftercare_agent.persistence import BuyerHistoryCursorRepository, CaseRepository, Database
from aftercare_agent.persistence.investigations import (
    InvestigationEgmBinding,
    InvestigationEgmBindingRepository,
    InvestigationEgmProjectionRepository,
    InvestigationEgmRevocationRepository,
    InvestigationObservationRepository,
)

from .egm import InvestigationEvidenceAdapter, UnboundEvidenceApplication

# Which connector answer becomes which evidence type.  An answer whose schema is
# not here is not a business fact anyone may cite, so the bridge refuses it
# rather than storing a shape the assessment cannot read.
SCHEMA_KINDS: Mapping[str, SourceKind] = {
    ORDER_FACTS_SCHEMA: SourceKind.ORDER_LEDGER,
    TRACKING_FACTS_SCHEMA: SourceKind.CARRIER,
    BUYER_FACTS_SCHEMA: SourceKind.BUYER_CHANNEL,
}
TOOL_SCHEMAS: Mapping[str, str] = {
    "lookup_order": ORDER_FACTS_SCHEMA,
    "lookup_tracking": TRACKING_FACTS_SCHEMA,
    "lookup_buyer_message": BUYER_FACTS_SCHEMA,
}
MAX_EXCERPT = 512
MAX_EVIDENCE_CONTEXT_ITEMS = 64
MAX_EVIDENCE_CONTEXT_BYTES = 65_536

# The audit identity of the process that speaks for the connector.  It is a
# deployment coordinate, not model input, so it is set here and never derived
# from a request.
DEFAULT_CONNECTOR_SUBJECT = "commerce-connector"


def registered_source_kinds(connector: CommerceConnector) -> Mapping[str, SourceKind]:
    """Map every source the connector speaks for to the kind its answers can be."""
    kinds: dict[str, SourceKind] = {}
    for tool, source_id in connector.registered_sources().items():
        schema = TOOL_SCHEMAS.get(tool)
        if schema is None:
            raise ContractViolation(ErrorCode.CONFLICT, "connector tool has no evidence schema")
        kinds[source_id] = SCHEMA_KINDS[schema]
    return kinds


def render_evidence_context(
    observations: Sequence[InvestigationEvidence],
    *,
    max_items: int = MAX_EVIDENCE_CONTEXT_ITEMS,
    max_bytes: int = MAX_EVIDENCE_CONTEXT_BYTES,
) -> str:
    """Render one complete, bounded ledger snapshot for a model input.

    The trusted host chooses the observations.  The model receives stable
    evidence IDs and normalized connector excerpts, but no authority: stale,
    revoked and conflicting rows remain visible and the deterministic gate
    still decides whether a later proposal is admissible.  Refusing an
    oversized snapshot is safer than truncating away a conflicting statement.
    """
    if type(max_items) is not int or max_items < 1:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "evidence context item limit is invalid")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "evidence context byte limit is invalid")
    ordered = sorted(observations, key=lambda item: item.evidence_id)
    if len(ordered) > max_items:
        raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "evidence context has too many items")
    expected_scope: tuple[str, str, str] | None = None
    items: list[dict[str, object]] = []
    for evidence in ordered:
        scope = (evidence.tenant_id, evidence.case_id, evidence.order_id)
        if expected_scope is None:
            expected_scope = scope
        elif scope != expected_scope:
            raise ContractViolation(ErrorCode.FORBIDDEN, "evidence context mixes cases or orders")
        try:
            excerpt = json.loads(evidence.original.excerpt)
        except (TypeError, ValueError) as exc:
            raise ContractViolation(
                ErrorCode.EVIDENCE_REJECTED, "evidence context excerpt is not structured JSON"
            ) from exc
        if not isinstance(excerpt, dict):
            raise ContractViolation(
                ErrorCode.EVIDENCE_REJECTED, "evidence context excerpt is not an object"
            )
        allowed_facts = {
            "order_snapshot": {
                "order_id",
                "status",
                "buyer_id",
                "payment_status",
                "payment_amount",
            },
            "logistics_observation": {
                "order_id",
                "carrier",
                "tracking_number",
                "delivered_at",
                "latest_event",
            },
            # Buyer free text is audit material, not model context.  The
            # normalized assertion carries the semantic fact without letting
            # an untrusted message become prompt instructions.
            "buyer_statement": {
                "order_id",
                "message_id",
                "channel",
                "received_at",
                "assertion",
            },
        }[evidence.kind]
        item: dict[str, object] = {
            "evidence_id": evidence.evidence_id,
            "kind": evidence.kind,
            "source_id": evidence.source_id,
            "observed_at": evidence.observed_at.isoformat(),
            "received_at": evidence.received_at.isoformat(),
            "revoked_at": (
                None if evidence.revoked_at is None else evidence.revoked_at.isoformat()
            ),
            "facts": {key: excerpt[key] for key in sorted(allowed_facts) if key in excerpt},
        }
        if isinstance(evidence, LogisticsObservation):
            item["delivery_status"] = evidence.delivery_status.value
        elif isinstance(evidence, BuyerStatement):
            item["assertion"] = evidence.assertion.value
        items.append(item)
    payload = {
        "schema": "aftercare.investigation-evidence-context.v1",
        "scope": (
            None
            if expected_scope is None
            else {
                "case_id": expected_scope[1],
                "order_id": expected_scope[2],
            }
        ),
        "observations": items,
    }
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(rendered.encode("utf-8")) > max_bytes:
        raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "evidence context is too large")
    return rendered


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
    elif schema == TRACKING_FACTS_SCHEMA:
        shipment = _section(body, "shipment")
        facts = {
            "order_id": body.get("order_id"),
            "carrier": body.get("carrier"),
            "tracking_number": body.get("tracking_number"),
            "delivered_at": shipment.get("delivered_at"),
            "latest_event": shipment.get("latest_event"),
        }
    else:
        message = _section(body, "message")
        facts = {
            "order_id": body.get("order_id"),
            "message_id": message.get("message_id"),
            "channel": message.get("channel"),
            "received_at": message.get("received_at"),
            "assertion": message.get("assertion"),
            "text": message.get("text"),
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
    if kind is SourceKind.BUYER_CHANNEL:
        message = _section(answer.body, "message")
        raw_assertion = message.get("assertion")
        if not isinstance(raw_assertion, str):
            raise ContractViolation(ErrorCode.EVIDENCE_REJECTED, "buyer answer has no assertion")
        try:
            assertion = BuyerAssertion(raw_assertion)
        except (TypeError, ValueError) as exc:
            raise ContractViolation(
                ErrorCode.EVIDENCE_REJECTED, "buyer answer has no supported assertion"
            ) from exc
        if message.get("message_id") is None or message.get("received_at") is None:
            raise ContractViolation(ErrorCode.EVIDENCE_REJECTED, "buyer answer has no message")
        return BuyerStatement(
            tenant_id=scope.tenant_id,
            case_id=scope.case_id,
            order_id=scope.order_id,
            evidence_id=evidence_id,
            source_id=answer.source_id,
            source_event_id=answer.source_event_id,
            observed_at=observed_at,
            received_at=observed_at,
            original=original,
            assertion=assertion,
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

    @property
    def scope(self) -> InvestigationScope:
        return self._scope

    def evidence_context(self) -> str:
        """Read and render the complete authorized ledger in one short transaction."""
        with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
            observations = self._adapter.persisted_observations(connection)
        return render_evidence_context(observations)

    def assess_persisted(
        self,
        connection: Any,
        proposal: InvestigationProposal,
        policy: FreshnessPolicy,
        *,
        now: datetime,
    ) -> InvestigationAssessment:
        """Assess the complete ledger on the Worker's fenced commit connection."""
        return self._adapter.assess_persisted(connection, proposal, policy, now=now)

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
        with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
            accepted = self._adapter.persist_observation(connection, evidence, grant, now=now)
        self._ingest_egm_if_bound(accepted, grant)
        return accepted

    def revoke(
        self,
        *,
        evidence_id: str,
        revoked_at: datetime,
        reason: str,
    ) -> InvestigationEvidence:
        """Persist a source correction and propagate its exact command to EGM."""
        application = self._adapter.application
        case_info = getattr(application, "case_info", None)
        revoke_evidence = getattr(application, "revoke_evidence", None)
        fingerprint = getattr(application, "fingerprint", None)
        if (
            not isinstance(fingerprint, str)
            or not callable(case_info)
            or not callable(revoke_evidence)
        ):
            with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
                return InvestigationObservationRepository().revoke(
                    connection,
                    scope=self._scope,
                    evidence_id=evidence_id,
                    revoked_at=revoked_at,
                    reason=reason,
                )

        projections = InvestigationEgmProjectionRepository()
        revocations = InvestigationEgmRevocationRepository()
        with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
            projection = projections.get(connection, scope=self._scope, evidence_id=evidence_id)
            existing = revocations.get(connection, scope=self._scope, evidence_id=evidence_id)
        if (
            projection is None
            or projection.completed_revision is None
            or projection.egm_evidence_id is None
        ):
            raise ContractViolation(
                ErrorCode.RETRYABLE, "evidence must finish EGM projection before revocation"
            )
        try:
            revision = (
                existing.expected_revision
                if existing is not None
                else self._egm_case_revision(case_info)
            )
        except ApplicationError as error:
            raise self._projection_error(error) from error
        operation_id = (
            "aftercare-investigation-revoke-" + sha256(evidence_id.encode()).hexdigest()[:32]
        )
        with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
            updated = InvestigationObservationRepository().revoke(
                connection,
                scope=self._scope,
                evidence_id=evidence_id,
                revoked_at=revoked_at,
                reason=reason,
            )
            revocation = revocations.reserve(
                connection,
                scope=self._scope,
                evidence_id=evidence_id,
                egm_evidence_id=projection.egm_evidence_id,
                operation_id=operation_id,
                reason=reason,
                revoked_at=revoked_at,
                expected_revision=revision,
            )
        if revocation.completed_revision is not None:
            return updated
        for attempt in range(2):
            try:
                result = self._adapter.revoke(
                    egm_evidence_id=revocation.egm_evidence_id,
                    operation_id=operation_id,
                    expected_revision=revocation.expected_revision,
                    reason=reason,
                )
            except ApplicationError as error:
                if error.code != "conflict" or attempt == 1:
                    raise self._projection_error(error) from error
                current_revision = self._egm_case_revision(case_info)
                with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
                    revocation = revocations.rebase(
                        connection,
                        scope=self._scope,
                        evidence_id=evidence_id,
                        operation_id=operation_id,
                        previous_revision=revocation.expected_revision,
                        expected_revision=current_revision,
                    )
                if revocation.completed_revision is not None:
                    return updated
                continue
            completed_revision, egm_revoked_at = self._egm_revocation_result(
                result,
                operation_id=operation_id,
                expected_revision=revocation.expected_revision,
                egm_evidence_id=revocation.egm_evidence_id,
            )
            with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
                revocations.complete(
                    connection,
                    scope=self._scope,
                    evidence_id=evidence_id,
                    operation_id=operation_id,
                    expected_revision=revocation.expected_revision,
                    completed_revision=completed_revision,
                    egm_revoked_at=egm_revoked_at,
                )
            return updated
        raise ContractViolation(ErrorCode.RETRYABLE, "EGM revocation outcome is unknown")

    def _ingest_egm_if_bound(self, evidence: InvestigationEvidence, grant: SourceGrant) -> None:
        """Project accepted ledger evidence into EGM when the host bound it.

        The two stores intentionally use separate short transactions.  The
        observation ledger remains the business snapshot; a failed EGM
        projection fails the tool step and the same evidence operation can be
        retried idempotently.
        """
        application = self._adapter.application
        fingerprint = getattr(application, "fingerprint", None)
        create_node = getattr(application, "create_node", None)
        case_info = getattr(application, "case_info", None)
        if not isinstance(fingerprint, str) or not callable(create_node) or not callable(case_info):
            return
        try:
            binding = self._egm_binding(application, fingerprint, create_node)
            operation_id = (
                "aftercare-investigation-evidence-"
                + sha256(evidence.evidence_id.encode()).hexdigest()[:32]
            )
            projections = InvestigationEgmProjectionRepository()
            with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
                projection = projections.get(
                    connection, scope=self._scope, evidence_id=evidence.evidence_id
                )
            if projection is None:
                revision = self._egm_case_revision(case_info)
                with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
                    projection = projections.reserve(
                        connection,
                        scope=self._scope,
                        evidence_id=evidence.evidence_id,
                        operation_id=operation_id,
                        expected_revision=revision,
                    )
            if projection.completed_revision is not None:
                return
            for attempt in range(2):
                try:
                    result = self._adapter.ingest(
                        evidence,
                        grant,
                        node_id=binding.node_id,
                        operation_id=operation_id,
                        expected_revision=projection.expected_revision,
                        now=evidence.received_at,
                    )
                except ApplicationError as error:
                    if error.code != "conflict" or attempt == 1:
                        raise
                    revision = self._egm_case_revision(case_info)
                    with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
                        projection = projections.rebase(
                            connection,
                            scope=self._scope,
                            evidence_id=evidence.evidence_id,
                            operation_id=operation_id,
                            previous_revision=projection.expected_revision,
                            expected_revision=revision,
                        )
                    if projection.completed_revision is not None:
                        return
                    continue
                completed_revision, egm_evidence_id = self._egm_projection_result(
                    result,
                    operation_id=operation_id,
                    expected_revision=projection.expected_revision,
                )
                with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
                    projections.complete(
                        connection,
                        scope=self._scope,
                        evidence_id=evidence.evidence_id,
                        operation_id=operation_id,
                        expected_revision=projection.expected_revision,
                        completed_revision=completed_revision,
                        egm_evidence_id=egm_evidence_id,
                    )
                return
        except ApplicationError as error:
            raise self._projection_error(error) from error

    @staticmethod
    def _projection_error(error: ApplicationError) -> ContractViolation:
        code = {
            "busy": ErrorCode.RETRYABLE,
            "conflict": ErrorCode.CONFLICT,
            "forbidden": ErrorCode.FORBIDDEN,
            "invalid": ErrorCode.EVIDENCE_REJECTED,
            "not_found": ErrorCode.CONFLICT,
        }.get(error.code, ErrorCode.RETRYABLE)
        return ContractViolation(code, f"EGM projection failed: {error}")

    def _egm_case_revision(self, case_info: Callable[..., object]) -> int:
        info = case_info(self._adapter.model_tools, self._scope.case_id)
        if not isinstance(info, Mapping) or type(info.get("revision")) is not int:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM case info has no revision")
        revision = info["revision"]
        assert isinstance(revision, int)
        return revision

    @staticmethod
    def _egm_projection_result(
        result: object, *, operation_id: str, expected_revision: int
    ) -> tuple[int, str]:
        if not isinstance(result, Mapping):
            raise ContractViolation(ErrorCode.CONFLICT, "EGM projection returned no envelope")
        revision = result.get("revision")
        if (
            result.get("operation_id") != operation_id
            or type(revision) is not int
            or revision <= expected_revision
        ):
            raise ContractViolation(
                ErrorCode.CONFLICT, "EGM projection returned an invalid receipt"
            )
        nested = result.get("result")
        egm_evidence_id = nested.get("id") if isinstance(nested, Mapping) else None
        if not isinstance(egm_evidence_id, str) or not egm_evidence_id:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM projection returned no evidence id")
        return revision, egm_evidence_id

    @staticmethod
    def _egm_revocation_result(
        result: object,
        *,
        operation_id: str,
        expected_revision: int,
        egm_evidence_id: str,
    ) -> tuple[int, datetime]:
        if not isinstance(result, Mapping):
            raise ContractViolation(ErrorCode.CONFLICT, "EGM revocation returned no envelope")
        revision = result.get("revision")
        nested = result.get("result")
        revoked_id = nested.get("evidence_id") if isinstance(nested, Mapping) else None
        raw_revoked_at = nested.get("revoked_at") if isinstance(nested, Mapping) else None
        if (
            result.get("operation_id") != operation_id
            or type(revision) is not int
            or revision <= expected_revision
            or revoked_id != egm_evidence_id
            or not isinstance(raw_revoked_at, str)
        ):
            raise ContractViolation(
                ErrorCode.CONFLICT, "EGM revocation returned an invalid receipt"
            )
        try:
            revoked_at = utc(datetime.fromisoformat(raw_revoked_at))
        except ValueError as error:
            raise ContractViolation(
                ErrorCode.CONFLICT, "EGM revocation returned an invalid timestamp"
            ) from error
        return revision, revoked_at

    def _egm_binding(
        self,
        application: Any,
        fingerprint: str,
        create_node: Callable[..., object],
    ) -> InvestigationEgmBinding:
        repository = InvestigationEgmBindingRepository()
        with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
            existing = repository.get(connection, scope=self._scope)
        if existing is not None:
            if existing.schema_fingerprint != fingerprint:
                raise ContractViolation(ErrorCode.CONFLICT, "EGM schema fingerprint changed")
            return existing
        operation_id = (
            "aftercare-investigation-node-"
            + sha256(f"{self._scope.tenant_id}:{self._scope.case_id}".encode()).hexdigest()[:32]
        )
        result = create_node(
            self._adapter.egm_writer,
            self._scope.case_id,
            {
                "operation_id": operation_id,
                "expected_revision": 0,
                "node_type": "investigation",
                "title": "Aftercare investigation",
                "anchors": {"order_id": self._scope.order_id},
            },
        )
        if not isinstance(result, Mapping):
            raise ContractViolation(ErrorCode.CONFLICT, "EGM node creation returned no envelope")
        nested = result.get("result")
        node_id = nested.get("id") if isinstance(nested, Mapping) else None
        if not isinstance(node_id, str) or not node_id:
            raise ContractViolation(ErrorCode.CONFLICT, "EGM node creation returned no node id")
        binding = InvestigationEgmBinding(
            tenant_id=self._scope.tenant_id,
            case_id=self._scope.case_id,
            order_id=self._scope.order_id,
            node_id=node_id,
            schema_fingerprint=fingerprint,
        )
        with self._database.transaction(tenant_id=self._scope.tenant_id) as connection:
            return repository.put(connection, binding)


class RunScopedEvidenceRecorder:
    """Record a Run's connector answers, resolving its Case scope on first use.

    The model Harness is built before a Run is claimed, so a recorder cannot be
    bound to one Case at construction time.  This resolves the Case's order when
    an answer actually arrives and refuses a Run whose Case row is missing:
    evidence has to belong to a Case the host admitted, not to whatever scope a
    step happened to carry.  A refusal is what keeps a mis-scoped Run from
    filing another order's facts under this Case -- ``evidence_from_answer``
    rejects an answer about a different order.
    """

    def __init__(
        self,
        *,
        database: Database,
        connector: CommerceConnector,
        subject: str = DEFAULT_CONNECTOR_SUBJECT,
        application_for: Callable[[Mapping[str, SourceKind]], Any] | None = None,
    ) -> None:
        if not subject.strip():
            raise ValueError("connector subject is required")
        self._database = database
        self._connector = connector
        self._subject = subject
        self._application_for = application_for
        # One grant per source the connector speaks for, taken from the
        # connector's own registry so a source it cannot answer for is never
        # granted.
        self._registered = registered_source_kinds(connector)
        self._memo: tuple[tuple[str, str], ConnectorEvidenceRecorder] | None = None

    @property
    def registered_sources(self) -> Mapping[str, str]:
        """Return the deployment-owned tool-to-source registry."""
        return dict(self._connector.registered_sources())

    def import_buyer_history(
        self,
        *,
        scope: RunScope,
        store: ContentAddressedArtifactStore,
        page_size: int = MAX_BUYER_MESSAGE_PAGE_SIZE,
    ) -> int:
        """Import every new buyer message through the normal evidence path.

        Evidence and EGM projection finish before the page cursor advances.
        A crash in between therefore replays stable source events and artifacts
        instead of skipping a message.  The cursor is host-owned and never
        appears in a model tool request.
        """
        recorder = self._recorder_for(scope)
        investigation_scope = recorder.scope
        source_id = self._connector.registered_sources()["lookup_buyer_message"]
        cursors = BuyerHistoryCursorRepository()
        with self._database.transaction(
            tenant_id=scope.tenant_id, subject_id=self._subject
        ) as connection:
            current = cursors.get(
                connection,
                scope=investigation_scope,
                source_id=source_id,
            )
        message_id = current.message_id if current is not None else None
        imported = 0
        while True:
            page = self._connector.lookup_buyer_messages(
                tenant_id=scope.tenant_id,
                order_id=investigation_scope.order_id,
                after_message_id=message_id,
                limit=page_size,
            )
            if not page.items:
                return imported
            for answer in page.items:
                artifact = store.put(
                    scope,
                    reference_id=f"buyer-history:{answer.source_event_id}",
                    content=answer.content(),
                )
                call_digest = sha256(answer.source_event_id.encode()).hexdigest()[:32]
                recorder(
                    request=ToolRequest(
                        call_id=f"buyer-history:{call_digest}",
                        name="lookup_buyer_message",
                        arguments_json="{}",
                    ),
                    scope=scope,
                    answer=answer,
                    artifact=artifact,
                )
                imported += 1
            last_message_id = page.last_message_id
            if last_message_id is None:
                raise ContractViolation(ErrorCode.CONFLICT, "buyer history page lost its cursor")
            with self._database.transaction(
                tenant_id=scope.tenant_id, subject_id=self._subject
            ) as connection:
                cursors.advance(
                    connection,
                    scope=investigation_scope,
                    source_id=source_id,
                    expected_message_id=message_id,
                    next_message_id=last_message_id,
                )
            message_id = last_message_id
            if page.next_cursor is None:
                return imported

    def __call__(
        self,
        *,
        request: ToolRequest,
        scope: RunScope,
        answer: ConnectorAnswer,
        artifact: ArtifactReference,
    ) -> InvestigationEvidence:
        recorder = self._recorder_for(scope)
        return recorder(request=request, scope=scope, answer=answer, artifact=artifact)

    def evidence_context(self, scope: RunScope) -> str:
        """Resolve the admitted Case and return its bounded ledger snapshot."""
        return self._recorder_for(scope).evidence_context()

    def revoke_evidence(
        self,
        scope: RunScope,
        *,
        evidence_id: str,
        revoked_at: datetime,
        reason: str,
    ) -> InvestigationEvidence:
        """Apply a trusted connector correction to the admitted Case."""
        return self._recorder_for(scope).revoke(
            evidence_id=evidence_id,
            revoked_at=revoked_at,
            reason=reason,
        )

    def assess_persisted(
        self,
        connection: Any,
        scope: RunScope,
        proposal: InvestigationProposal,
        policy: FreshnessPolicy,
        *,
        now: datetime,
    ) -> InvestigationAssessment:
        """Run the deterministic gate inside the Worker's final transaction."""
        return self._recorder_for(scope).assess_persisted(connection, proposal, policy, now=now)

    def _recorder_for(self, scope: RunScope) -> ConnectorEvidenceRecorder:
        key = (scope.tenant_id, scope.case_id)
        # One Case at a time: a Worker's steps stay on the Run it holds, so a
        # single-entry memo is enough and cannot grow with the daemon's uptime.
        if self._memo is None or self._memo[0] != key:
            self._memo = (key, self._build(key))
        return self._memo[1]

    def _build(self, key: tuple[str, str]) -> ConnectorEvidenceRecorder:
        tenant_id, case_id = key
        with self._database.transaction(
            tenant_id=tenant_id, subject_id=self._subject
        ) as connection:
            case = CaseRepository().get_case(connection, tenant_id, case_id)
        if case is None:
            raise ContractViolation(ErrorCode.FORBIDDEN, "evidence arrived for an unknown Case")
        scope = InvestigationScope(tenant_id=tenant_id, case_id=case_id, order_id=case.order_id)
        application = (
            self._application_for(self._registered)
            if self._application_for is not None
            else UnboundEvidenceApplication()
        )
        writer_permissions = {"context:read", "task:write", "evidence:write"}
        if callable(getattr(application, "revoke_evidence", None)):
            writer_permissions.add("evidence:revoke")
        adapter = InvestigationEvidenceAdapter(
            application,
            scope,
            connector=Principal(
                subject=self._subject,
                tenant_id=tenant_id,
                permissions=frozenset({"evidence:write"}),
                case_ids=frozenset({case_id}),
                source_systems=frozenset(self._registered),
            ),
            model_tools=Principal(
                subject="model-tools",
                tenant_id=tenant_id,
                permissions=frozenset({"context:read"}),
                case_ids=frozenset({case_id}),
                source_systems=frozenset(),
            ),
            egm_writer=Principal(
                subject="aftercare-egm",
                tenant_id=tenant_id,
                permissions=frozenset(writer_permissions),
                case_ids=frozenset({case_id}),
                source_systems=frozenset(self._registered),
            ),
            registered_sources=self._registered,
        )
        return ConnectorEvidenceRecorder(
            database=self._database,
            adapter=adapter,
            grants={
                source_id: SourceGrant(
                    **scope.model_dump(), subject_id=self._subject, source_id=source_id
                )
                for source_id in self._registered
            },
        )


__all__ = [
    "ConnectorEvidenceRecorder",
    "DEFAULT_CONNECTOR_SUBJECT",
    "MAX_EXCERPT",
    "MAX_EVIDENCE_CONTEXT_BYTES",
    "MAX_EVIDENCE_CONTEXT_ITEMS",
    "RunScopedEvidenceRecorder",
    "SCHEMA_KINDS",
    "TOOL_SCHEMAS",
    "evidence_from_answer",
    "registered_source_kinds",
    "render_evidence_context",
]
