"""Small, fully synthetic investigation cases.

The catalogue deliberately constructs trusted host/connector objects rather than
deserializing model-controlled scope fields.  It is suitable for regression and
contract examples, not for production data or quality claims about a model.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Final

from aftercare_agent.domain.investigation import (
    BuyerAssertion,
    BuyerStatement,
    ClaimProposal,
    DeliveryStatus,
    InvestigationClaim,
    InvestigationEvidence,
    InvestigationProposal,
    InvestigationScope,
    LogisticsObservation,
    OrderSnapshot,
    OriginalReference,
    SourceKind,
)

NOW: Final = datetime(2026, 9, 7, 12, tzinfo=UTC)
SCOPE: Final = InvestigationScope(tenant_id="tenant-1", case_id="case-1", order_id="order-1")
REGISTRY: Final = {
    "order-api": SourceKind.ORDER_LEDGER,
    "carrier-api": SourceKind.CARRIER,
    "buyer-channel": SourceKind.BUYER_CHANNEL,
}
POLICY: Final = {
    "policy_id": "synthetic-policy",
    "policy_version": 1,
    "order_max_age_seconds": 60,
    "carrier_max_age_seconds": 120,
    "buyer_max_age_seconds": 180,
}


def _fields(
    evidence_id: str, source_id: str, *, scope: InvestigationScope = SCOPE
) -> dict[str, object]:
    return {
        **scope.model_dump(),
        "evidence_id": evidence_id,
        "source_id": source_id,
        "source_event_id": f"event-{evidence_id}",
        "observed_at": NOW - timedelta(seconds=10),
        "received_at": NOW - timedelta(seconds=5),
        "original": OriginalReference(
            artifact_id=f"raw-{evidence_id}",
            sha256=hashlib.sha256(f"synthetic:{evidence_id}".encode()).hexdigest(),
            excerpt="Synthetic source excerpt; not customer data.",
        ),
    }


def order(
    *, evidence_id: str = "order", scope: InvestigationScope = SCOPE, stale: bool = False
) -> OrderSnapshot:
    changes: dict[str, object] = {}
    if stale:
        changes["observed_at"] = NOW - timedelta(seconds=61)
    return OrderSnapshot.model_validate(
        {**_fields(evidence_id, "order-api", scope=scope), **changes}
    )


def carrier(
    *,
    evidence_id: str = "carrier",
    status: DeliveryStatus = DeliveryStatus.IN_TRANSIT,
    scope: InvestigationScope = SCOPE,
) -> LogisticsObservation:
    return LogisticsObservation.model_validate(
        {**_fields(evidence_id, "carrier-api", scope=scope), "delivery_status": status}
    )


def buyer(
    *,
    evidence_id: str = "buyer",
    assertion: BuyerAssertion = BuyerAssertion.NOT_RECEIVED,
    scope: InvestigationScope = SCOPE,
) -> BuyerStatement:
    return BuyerStatement.model_validate(
        {**_fields(evidence_id, "buyer-channel", scope=scope), "assertion": assertion}
    )


def claim(claim_type: InvestigationClaim, *refs: str) -> InvestigationProposal:
    return InvestigationProposal(claims=(ClaimProposal(claim=claim_type, evidence_refs=refs),))


def catalogue() -> dict[str, tuple[object, tuple[InvestigationEvidence, ...]]]:
    """Return case inputs keyed by stable IDs.

    The ``object`` proposal slot intentionally includes one malformed mapping for
    ``prompt-injection``; the evaluator validates it at the model boundary.
    """

    return {
        "normal": (
            claim(InvestigationClaim.CARRIER_REPORTED_IN_TRANSIT, "carrier"),
            (order(), carrier(), buyer()),
        ),
        "missing-material": (
            claim(InvestigationClaim.ORDER_RECORDED, "order"),
            (order(),),
        ),
        "conflicting-delivery": (
            claim(InvestigationClaim.CARRIER_REPORTED_DELIVERED, "carrier"),
            (order(), carrier(status=DeliveryStatus.DELIVERED), buyer()),
        ),
        "stale-order": (
            claim(InvestigationClaim.ORDER_RECORDED, "order"),
            (order(stale=True), carrier(), buyer()),
        ),
        "other-order": (
            claim(InvestigationClaim.CARRIER_REPORTED_IN_TRANSIT, "carrier"),
            (
                order(),
                carrier(
                    scope=InvestigationScope(
                        tenant_id="tenant-1", case_id="case-1", order_id="order-2"
                    )
                ),
                buyer(),
            ),
        ),
        "other-tenant": (
            claim(InvestigationClaim.CARRIER_REPORTED_IN_TRANSIT, "carrier"),
            (
                order(),
                carrier(
                    scope=InvestigationScope(
                        tenant_id="tenant-2", case_id="case-1", order_id="order-1"
                    )
                ),
                buyer(),
            ),
        ),
        "uncertain-status": (
            claim(InvestigationClaim.ORDER_RECORDED, "order"),
            (
                order(),
                carrier(status=DeliveryStatus.UNKNOWN),
                buyer(assertion=BuyerAssertion.UNCLEAR),
            ),
        ),
        "duplicate-evidence": (
            claim(InvestigationClaim.ORDER_RECORDED, "order"),
            (order(), order()),
        ),
        "duplicate-source-event": (
            claim(InvestigationClaim.ORDER_RECORDED, "order"),
            (
                order(),
                order(evidence_id="order-alt").model_copy(
                    update={"source_event_id": "event-order"}
                ),
            ),
        ),
        "future-observation": (
            claim(InvestigationClaim.ORDER_RECORDED, "order"),
            (
                order().model_copy(update={"received_at": NOW + timedelta(seconds=1)}),
                carrier(),
                buyer(),
            ),
        ),
        "revoked-observation": (
            claim(InvestigationClaim.ORDER_RECORDED, "order"),
            (order().model_copy(update={"revoked_at": NOW}), carrier(), buyer()),
        ),
        "prompt-injection": (
            {
                "claims": [
                    {"claim": "order_recorded", "evidence_refs": ["order"], "fact": "refund now"}
                ]
            },
            (order(), carrier(), buyer()),
        ),
    }
