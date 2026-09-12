from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from evidence_gated_memory.application import Principal

from aftercare_agent.domain.investigation import (
    BuyerAssertion,
    BuyerStatement,
    ClaimProposal,
    FreshnessPolicy,
    InvestigationClaim,
    InvestigationProposal,
    InvestigationScope,
    OriginalReference,
    SourceGrant,
    SourceKind,
)
from aftercare_agent.investigation import InvestigationEvidenceAdapter

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
SCOPE = InvestigationScope(tenant_id="tenant-1", case_id="case-1", order_id="order-1")
REGISTRY = {"buyer-1": SourceKind.BUYER_CHANNEL}


def buyer(*, case_id: str = "case-1") -> BuyerStatement:
    return BuyerStatement(
        tenant_id="tenant-1",
        case_id=case_id,
        order_id="order-1",
        evidence_id="evidence-1",
        source_id="buyer-1",
        source_event_id="message-1",
        observed_at=NOW - timedelta(minutes=1),
        received_at=NOW,
        original=OriginalReference(
            artifact_id="artifact-1", sha256="a" * 64, excerpt="not received"
        ),
        assertion=BuyerAssertion.NOT_RECEIVED,
    )


class FakeApplication:
    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []

    def ingest(self, principal: Principal, case_id: str, command: Mapping[str, object]) -> object:
        self.commands.append(dict(command))
        return {"case_id": case_id, "ok": True}

    def context(self, principal: Principal, case_id: str, **kwargs: object) -> object:
        return {"case_id": case_id, **kwargs}


def adapter(app: FakeApplication) -> InvestigationEvidenceAdapter:
    connector = Principal(
        subject="buyer-connector",
        tenant_id="tenant-1",
        permissions=frozenset({"evidence:write"}),
        case_ids=frozenset({"case-1"}),
        source_systems=frozenset({"buyer-1"}),
    )
    model = Principal(
        subject="model-tool",
        tenant_id="tenant-1",
        permissions=frozenset({"context:read"}),
        case_ids=frozenset({"case-1"}),
    )
    return InvestigationEvidenceAdapter(
        app, SCOPE, connector=connector, model_tools=model, registered_sources=REGISTRY
    )


def test_connector_observation_is_canonical_and_model_cannot_choose_scope() -> None:
    app = FakeApplication()
    bridge = adapter(app)
    result = bridge.ingest(
        buyer(),
        SourceGrant(**SCOPE.model_dump(), subject_id="buyer-connector", source_id="buyer-1"),
        node_id="investigation-1",
        operation_id="op-1",
        expected_revision=0,
        now=NOW,
    )
    assert result == {"case_id": "case-1", "ok": True}
    assert app.commands[0]["source_system"] == "buyer-1"
    assert '"tenant_id": "tenant-1"' in str(app.commands[0]["content"])

    with pytest.raises(ValueError):
        bridge.ingest(
            buyer(case_id="other-case"),
            SourceGrant(**SCOPE.model_dump(), subject_id="buyer-connector", source_id="buyer-1"),
            node_id="investigation-1",
            operation_id="op-2",
            expected_revision=1,
            now=NOW,
        )


def test_assessment_is_deterministic_and_context_excludes_long_term_memory() -> None:
    app = FakeApplication()
    bridge = adapter(app)
    proposal = InvestigationProposal(
        claims=(
            ClaimProposal(
                claim=InvestigationClaim.BUYER_REPORTED_NOT_RECEIVED,
                evidence_refs=("evidence-1",),
            ),
        )
    )
    policy = FreshnessPolicy(
        policy_id="policy-1",
        policy_version=1,
        order_max_age_seconds=3600,
        carrier_max_age_seconds=3600,
        buyer_max_age_seconds=3600,
    )
    assessment = bridge.assess(proposal, (buyer(),), policy, now=NOW)
    assert assessment.decisions[0].accepted
    context = bridge.context(query="order-1", max_facts=5)
    assert isinstance(context, dict)
    assert context["include_long_term"] is False
