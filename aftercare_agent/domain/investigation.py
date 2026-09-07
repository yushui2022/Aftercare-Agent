"""A0 investigation contracts and deterministic gates, not an EGM projection."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from aftercare_agent.domain.common import (
    CaseScope,
    ContractModel,
    ContractViolation,
    ErrorCode,
    Identifier,
    PositiveInt,
    SchemaVersion,
    Sha256,
    UtcDatetime,
    require_same_case,
    utc,
)


class InvestigationScope(CaseScope):
    order_id: Identifier


class SourceKind(StrEnum):
    ORDER_LEDGER = "order_ledger"
    CARRIER = "carrier"
    BUYER_CHANNEL = "buyer_channel"


class SourceGrant(InvestigationScope):
    """Host authorization result; never deserialize this from a model tool request."""

    subject_id: Identifier
    source_id: Identifier


class OriginalReference(ContractModel):
    artifact_id: Identifier
    sha256: Sha256
    excerpt: Annotated[str, Field(min_length=1, max_length=4096)]


class Observation(InvestigationScope):
    schema_version: SchemaVersion = 1
    evidence_id: Identifier
    source_id: Identifier
    source_event_id: Identifier
    observed_at: UtcDatetime
    received_at: UtcDatetime
    original: OriginalReference
    revoked_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def chronological(self) -> Self:
        if self.observed_at > self.received_at:
            raise ValueError("observation cannot follow its original receipt time")
        if self.revoked_at is not None and self.revoked_at < self.received_at:
            raise ValueError("revocation cannot precede ingestion")
        return self


class OrderSnapshot(Observation):
    kind: Literal["order_snapshot"] = "order_snapshot"


class DeliveryStatus(StrEnum):
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    UNKNOWN = "unknown"


class LogisticsObservation(Observation):
    kind: Literal["logistics_observation"] = "logistics_observation"
    delivery_status: DeliveryStatus


class BuyerAssertion(StrEnum):
    NOT_RECEIVED = "not_received"
    RECEIVED = "received"
    UNCLEAR = "unclear"


class BuyerStatement(Observation):
    kind: Literal["buyer_statement"] = "buyer_statement"
    assertion: BuyerAssertion


type InvestigationEvidence = Annotated[
    OrderSnapshot | LogisticsObservation | BuyerStatement, Field(discriminator="kind")
]


class FreshnessPolicy(ContractModel):
    """All ages are explicit seconds; there is deliberately no production TTL default."""

    policy_id: Identifier
    policy_version: PositiveInt
    order_max_age_seconds: PositiveInt
    carrier_max_age_seconds: PositiveInt
    buyer_max_age_seconds: PositiveInt

    def max_age(self, kind: SourceKind) -> int:
        return {
            SourceKind.ORDER_LEDGER: self.order_max_age_seconds,
            SourceKind.CARRIER: self.carrier_max_age_seconds,
            SourceKind.BUYER_CHANNEL: self.buyer_max_age_seconds,
        }[kind]


class InvestigationClaim(StrEnum):
    ORDER_RECORDED = "order_recorded"
    CARRIER_REPORTED_DELIVERED = "carrier_reported_delivered"
    CARRIER_REPORTED_IN_TRANSIT = "carrier_reported_in_transit"
    BUYER_REPORTED_NOT_RECEIVED = "buyer_reported_not_received"
    BUYER_REPORTED_RECEIVED = "buyer_reported_received"


class ClaimProposal(ContractModel):
    claim: InvestigationClaim
    evidence_refs: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("duplicate evidence reference")
        return self


class InvestigationProposal(ContractModel):
    """The whole model-visible payload: no scope, source, amount, or fact prose."""

    schema_version: SchemaVersion = 1
    claims: Annotated[tuple[ClaimProposal, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def unique_claims(self) -> Self:
        if len({proposal.claim for proposal in self.claims}) != len(self.claims):
            raise ValueError("duplicate claim")
        return self


class EvidenceIssue(StrEnum):
    FUTURE = "future"
    STALE = "stale"
    REVOKED = "revoked"
    UNKNOWN_REFERENCE = "unknown_reference"
    CLAIM_NOT_SUPPORTED = "claim_not_supported"


class RejectedReference(ContractModel):
    evidence_id: Identifier
    reason: EvidenceIssue


class Citation(ContractModel):
    evidence_id: Identifier
    source_id: Identifier
    observed_at: UtcDatetime
    received_at: UtcDatetime
    original: OriginalReference


class ClaimDecision(ContractModel):
    claim: InvestigationClaim
    accepted: bool
    citations: tuple[Citation, ...]
    rejected: tuple[RejectedReference, ...]


class MissingMaterial(StrEnum):
    ORDER_SNAPSHOT = "order_snapshot"
    CARRIER_OBSERVATION = "carrier_observation"
    BUYER_STATEMENT = "buyer_statement"
    CLEAR_CARRIER_STATUS = "clear_carrier_status"
    CLEAR_BUYER_STATEMENT = "clear_buyer_statement"


class ConflictKind(StrEnum):
    CARRIER_BUYER_DISAGREEMENT = "carrier_buyer_disagreement"
    BUYER_STATEMENTS_DISAGREE = "buyer_statements_disagree"


class EvidenceConflict(ContractModel):
    kind: ConflictKind
    evidence_refs: tuple[Identifier, ...]


class InvestigationDisposition(StrEnum):
    NEEDS_MATERIAL = "needs_material"
    HUMAN_REVIEW = "human_review"
    RECOMMENDATION_READY = "recommendation_ready"


class InvestigationAssessment(InvestigationScope):
    schema_version: SchemaVersion = 1
    policy_id: Identifier
    policy_version: PositiveInt
    evaluated_at: UtcDatetime
    disposition: InvestigationDisposition
    decisions: tuple[ClaimDecision, ...]
    missing: tuple[MissingMaterial, ...]
    conflicts: tuple[EvidenceConflict, ...]
    unavailable: tuple[RejectedReference, ...]
    authorizes_external_action: Literal[False] = False
    closes_case: Literal[False] = False


def source_kind(evidence: InvestigationEvidence) -> SourceKind:
    if isinstance(evidence, OrderSnapshot):
        return SourceKind.ORDER_LEDGER
    if isinstance(evidence, LogisticsObservation):
        return SourceKind.CARRIER
    return SourceKind.BUYER_CHANNEL


def _require_order(expected: InvestigationScope, actual: InvestigationScope) -> None:
    require_same_case(expected, actual)
    if expected.order_id != actual.order_id:
        raise ContractViolation(ErrorCode.FORBIDDEN, "order scope mismatch")


def _require_registered(
    evidence: InvestigationEvidence, registered_sources: Mapping[str, SourceKind]
) -> None:
    if registered_sources.get(evidence.source_id) != source_kind(evidence):
        raise ContractViolation(ErrorCode.FORBIDDEN, "source has no registered capability")


def ingest_observation(
    expected: InvestigationScope,
    grant: SourceGrant,
    registered_sources: Mapping[str, SourceKind],
    evidence: InvestigationEvidence,
    *,
    now: datetime,
    existing: InvestigationEvidence | None = None,
) -> InvestigationEvidence:
    """Validate normalized connector input; host persists/deduplicates transactionally later."""
    _require_order(expected, grant)
    _require_order(expected, evidence)
    if grant.source_id != evidence.source_id:
        raise ContractViolation(ErrorCode.FORBIDDEN, "source not granted to caller")
    _require_registered(evidence, registered_sources)
    current = utc(now)
    if evidence.received_at > current or (
        evidence.revoked_at is not None and evidence.revoked_at > current
    ):
        raise ContractViolation(ErrorCode.EVIDENCE_REJECTED, "future evidence timestamp")
    if existing is not None:
        _require_order(expected, existing)
        # Host looks up (tenant, case, source_id, source_event_id), not a model-chosen ID.
        if existing != evidence:
            raise ContractViolation(ErrorCode.CONFLICT, "replay must preserve original evidence")
        return existing
    return evidence


def evidence_issue(
    evidence: InvestigationEvidence, policy: FreshnessPolicy, *, now: datetime
) -> EvidenceIssue | None:
    current = utc(now)
    if evidence.received_at > current or (
        evidence.revoked_at is not None and evidence.revoked_at > current
    ):
        return EvidenceIssue.FUTURE
    if evidence.revoked_at is not None:
        return EvidenceIssue.REVOKED
    if (current - evidence.observed_at).total_seconds() > policy.max_age(source_kind(evidence)):
        return EvidenceIssue.STALE
    return None


def _supported_claim(evidence: InvestigationEvidence) -> InvestigationClaim | None:
    if isinstance(evidence, OrderSnapshot):
        return InvestigationClaim.ORDER_RECORDED
    if isinstance(evidence, LogisticsObservation):
        return {
            DeliveryStatus.DELIVERED: InvestigationClaim.CARRIER_REPORTED_DELIVERED,
            DeliveryStatus.IN_TRANSIT: InvestigationClaim.CARRIER_REPORTED_IN_TRANSIT,
        }.get(evidence.delivery_status)
    return {
        BuyerAssertion.NOT_RECEIVED: InvestigationClaim.BUYER_REPORTED_NOT_RECEIVED,
        BuyerAssertion.RECEIVED: InvestigationClaim.BUYER_REPORTED_RECEIVED,
    }.get(evidence.assertion)


def assess_investigation(
    expected: InvestigationScope,
    proposal: InvestigationProposal,
    observations: Sequence[InvestigationEvidence],
    registered_sources: Mapping[str, SourceKind],
    policy: FreshnessPolicy,
    *,
    now: datetime,
) -> InvestigationAssessment:
    """Evaluate the full authorized ledger snapshot, not a subset selected by the model."""
    current = utc(now)
    evidence_by_id: dict[str, InvestigationEvidence] = {}
    source_events: set[tuple[str, str]] = set()
    unavailable: dict[str, EvidenceIssue] = {}
    claims: dict[InvestigationClaim, list[str]] = {}
    available_kinds: set[SourceKind] = set()
    for evidence in sorted(observations, key=lambda item: item.evidence_id):
        _require_order(expected, evidence)
        _require_registered(evidence, registered_sources)
        if evidence.evidence_id in evidence_by_id:
            raise ContractViolation(ErrorCode.CONFLICT, "duplicate ledger evidence ID")
        source_event = (evidence.source_id, evidence.source_event_id)
        if source_event in source_events:
            raise ContractViolation(ErrorCode.CONFLICT, "source event has multiple evidence IDs")
        source_events.add(source_event)
        evidence_by_id[evidence.evidence_id] = evidence
        issue = evidence_issue(evidence, policy, now=current)
        if issue is not None:
            unavailable[evidence.evidence_id] = issue
        else:
            available_kinds.add(source_kind(evidence))
            claim = _supported_claim(evidence)
            if claim is not None:
                claims.setdefault(claim, []).append(evidence.evidence_id)

    decisions: list[ClaimDecision] = []
    for selected in proposal.claims:
        rejected: list[RejectedReference] = []
        citations: list[Citation] = []
        for ref in sorted(selected.evidence_refs):
            observed = evidence_by_id.get(ref)
            reason = unavailable.get(ref)
            if observed is None:
                reason = EvidenceIssue.UNKNOWN_REFERENCE
            elif reason is None and _supported_claim(observed) != selected.claim:
                reason = EvidenceIssue.CLAIM_NOT_SUPPORTED
            if reason is not None:
                rejected.append(RejectedReference(evidence_id=ref, reason=reason))
            elif observed is not None:
                citations.append(
                    Citation(
                        evidence_id=ref,
                        source_id=observed.source_id,
                        observed_at=observed.observed_at,
                        received_at=observed.received_at,
                        original=observed.original,
                    )
                )
        decisions.append(
            ClaimDecision(
                claim=selected.claim,
                accepted=not rejected,
                citations=tuple(citations),
                rejected=tuple(rejected),
            )
        )

    missing: list[MissingMaterial] = []
    for kind, material in (
        (SourceKind.ORDER_LEDGER, MissingMaterial.ORDER_SNAPSHOT),
        (SourceKind.CARRIER, MissingMaterial.CARRIER_OBSERVATION),
        (SourceKind.BUYER_CHANNEL, MissingMaterial.BUYER_STATEMENT),
    ):
        if kind not in available_kinds:
            missing.append(material)
    if SourceKind.CARRIER in available_kinds and not (
        InvestigationClaim.CARRIER_REPORTED_DELIVERED in claims
        or InvestigationClaim.CARRIER_REPORTED_IN_TRANSIT in claims
    ):
        missing.append(MissingMaterial.CLEAR_CARRIER_STATUS)
    if SourceKind.BUYER_CHANNEL in available_kinds and not (
        InvestigationClaim.BUYER_REPORTED_NOT_RECEIVED in claims
        or InvestigationClaim.BUYER_REPORTED_RECEIVED in claims
    ):
        missing.append(MissingMaterial.CLEAR_BUYER_STATEMENT)

    conflicts: list[EvidenceConflict] = []
    for left, right, conflict_kind in (
        (
            InvestigationClaim.CARRIER_REPORTED_DELIVERED,
            InvestigationClaim.BUYER_REPORTED_NOT_RECEIVED,
            ConflictKind.CARRIER_BUYER_DISAGREEMENT,
        ),
        (
            InvestigationClaim.BUYER_REPORTED_RECEIVED,
            InvestigationClaim.BUYER_REPORTED_NOT_RECEIVED,
            ConflictKind.BUYER_STATEMENTS_DISAGREE,
        ),
    ):
        if left in claims and right in claims:
            conflicts.append(
                EvidenceConflict(
                    kind=conflict_kind, evidence_refs=tuple(sorted(claims[left] + claims[right]))
                )
            )
    disposition = InvestigationDisposition.RECOMMENDATION_READY
    if missing:
        disposition = InvestigationDisposition.NEEDS_MATERIAL
    if conflicts or any(not decision.accepted for decision in decisions):
        disposition = InvestigationDisposition.HUMAN_REVIEW
    return InvestigationAssessment(
        **expected.model_dump(),
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        evaluated_at=current,
        disposition=disposition,
        decisions=tuple(decisions),
        missing=tuple(missing),
        conflicts=tuple(conflicts),
        unavailable=tuple(
            RejectedReference(evidence_id=ref, reason=reason) for ref, reason in unavailable.items()
        ),
    )
