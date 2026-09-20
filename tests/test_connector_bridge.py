"""Invariants of the connector-answer to evidence bridge."""

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from aftercare_agent.connectors import (
    CommerceConnector,
    CommerceSources,
    ConnectorAnswer,
    load_commerce_dataset,
)
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import (
    BuyerStatement,
    DeliveryStatus,
    InvestigationEvidence,
    InvestigationScope,
    LogisticsObservation,
    OrderSnapshot,
    SourceKind,
)
from aftercare_agent.domain.protocol import ArtifactReference
from aftercare_agent.investigation.connector_bridge import (
    evidence_from_answer,
    registered_source_kinds,
    render_evidence_context,
)

SAMPLE = Path(str(files("aftercare_agent.connectors").joinpath("data/commerce-sample.json")))
SOURCES = CommerceSources(order_ledger="erp-api", carrier="carrier-api", buyer_channel="in-app")
TENANT = "tenant-demo"
SCOPE = InvestigationScope(tenant_id=TENANT, case_id="case-1", order_id="A-1001")


def _connector() -> CommerceConnector:
    return CommerceConnector(load_commerce_dataset(SAMPLE), sources=SOURCES)


def _evidence(
    answer: ConnectorAnswer,
    *,
    scope: InvestigationScope = SCOPE,
    digest: str | None = None,
    registry: Mapping[str, SourceKind] | None = None,
) -> InvestigationEvidence:
    return evidence_from_answer(
        answer,
        scope=scope,
        artifact=ArtifactReference(
            tenant_id=SCOPE.tenant_id,
            case_id=SCOPE.case_id,
            reference_id="tool-result:call-1",
            sha256=digest or answer.digest(),
        ),
        registered_sources=registered_source_kinds(_connector()) if registry is None else registry,
    )


def _overrides(**updates: str) -> InvestigationScope:
    return SCOPE.model_copy(update=updates)


def test_an_order_answer_becomes_evidence_with_the_bytes_that_answered_it() -> None:
    answer = _connector().lookup_order(tenant_id=TENANT, order_id="A-1001")
    evidence = _evidence(answer)
    assert isinstance(evidence, OrderSnapshot)
    assert evidence.evidence_id == f"{answer.source_id}:{answer.source_event_id}"
    assert evidence.original.sha256 == answer.digest()
    assert "A-1001" in evidence.original.excerpt
    # The export is the receipt, not the clock: a replayed fetch has to be equal
    # to the first one, or the ledger reports a conflict instead of one fact.
    assert evidence.received_at == evidence.observed_at == answer.observed_at
    assert registered_source_kinds(_connector()) == {
        SOURCES.order_ledger: SourceKind.ORDER_LEDGER,
        SOURCES.carrier: SourceKind.CARRIER,
        SOURCES.buyer_channel: SourceKind.BUYER_CHANNEL,
    }


def test_a_tracking_answer_carries_the_carrier_verdict_not_a_claim() -> None:
    answer = _connector().lookup_tracking(tenant_id=TENANT, order_id="A-1001")
    evidence = _evidence(answer)
    assert isinstance(evidence, LogisticsObservation)
    assert evidence.delivery_status is DeliveryStatus.DELIVERED


def test_a_buyer_answer_becomes_a_statement_without_becoming_a_fact_of_receipt() -> None:
    answer = _connector().lookup_buyer_message(tenant_id=TENANT, order_id="A-1001")
    evidence = _evidence(answer)
    assert isinstance(evidence, BuyerStatement)
    assert evidence.assertion.value == "not_received"
    assert evidence.observed_at == answer.observed_at


def test_model_context_keeps_every_normalized_fact_but_excludes_buyer_prose() -> None:
    connector = _connector()
    buyer = _evidence(connector.lookup_buyer_message(tenant_id=TENANT, order_id="A-1001"))
    order = _evidence(connector.lookup_order(tenant_id=TENANT, order_id="A-1001"))

    payload = json.loads(render_evidence_context((order, buyer)))
    assert payload["schema"] == "aftercare.investigation-evidence-context.v1"
    assert payload["scope"] == {"case_id": SCOPE.case_id, "order_id": SCOPE.order_id}
    observations = payload["observations"]
    assert [item["evidence_id"] for item in observations] == sorted(
        (buyer.evidence_id, order.evidence_id)
    )
    buyer_context = next(item for item in observations if item["kind"] == "buyer_statement")
    assert buyer_context["assertion"] == "not_received"
    assert "text" not in buyer_context["facts"]
    with pytest.raises(ContractViolation) as error:
        render_evidence_context((order, buyer), max_items=1)
    assert error.value.code is ErrorCode.BUDGET_EXHAUSTED


def test_every_answer_that_cannot_become_evidence_is_refused() -> None:
    order = _connector().lookup_order(tenant_id=TENANT, order_id="A-1001")
    unknown = replace(order, body={**order.body, "schema": "aftercare.unknown.v1"})
    refused: tuple[tuple[Callable[[], InvestigationEvidence], ErrorCode], ...] = (
        (lambda: _evidence(order, digest="b" * 64), ErrorCode.CONFLICT),
        (lambda: _evidence(order, scope=_overrides(case_id="case-2")), ErrorCode.FORBIDDEN),
        (lambda: _evidence(order, scope=_overrides(order_id="A-9999")), ErrorCode.FORBIDDEN),
        (
            lambda: _evidence(order, registry={SOURCES.order_ledger: SourceKind.CARRIER}),
            ErrorCode.FORBIDDEN,
        ),
        (lambda: _evidence(unknown), ErrorCode.EVIDENCE_REJECTED),
    )
    for call, code in refused:
        with pytest.raises(ContractViolation) as error:
            call()
        assert error.value.code is code
