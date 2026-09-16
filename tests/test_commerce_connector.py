from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError

from aftercare_agent.connectors import (
    MAX_TRACKING_EVENTS,
    ORDER_FACTS_SCHEMA,
    TRACKING_FACTS_SCHEMA,
    CommerceConnector,
    CommerceDataset,
    CommerceSources,
    load_commerce_dataset,
)
from aftercare_agent.domain.common import ContractViolation, ErrorCode

SAMPLE = Path(str(files("aftercare_agent.connectors").joinpath("data/commerce-sample.json")))
TENANT = "tenant-demo"
SOURCES = CommerceSources(order_ledger="erp-api", carrier="carrier-api", buyer_channel="in-app")


def _connector() -> CommerceConnector:
    return CommerceConnector(load_commerce_dataset(SAMPLE), sources=SOURCES)


def test_the_shipped_export_parses_and_answers_both_tools() -> None:
    connector = _connector()
    order = connector.lookup_order(tenant_id=TENANT, order_id="A-1001")
    tracking = connector.lookup_tracking(tenant_id=TENANT, order_id="A-1001")
    assert order.body["schema"] == ORDER_FACTS_SCHEMA
    assert tracking.body["schema"] == TRACKING_FACTS_SCHEMA
    payment = order.body["payment"]
    shipment = tracking.body["shipment"]
    assert isinstance(payment, dict) and isinstance(shipment, dict)
    assert payment["amount"] == {"amount_minor": "95700", "currency": "CNY"}
    latest = shipment["latest_event"]
    assert isinstance(latest, dict)
    assert latest["code"] == "delivered"


def test_the_same_question_answers_with_identical_bytes() -> None:
    connector = _connector()
    first = connector.lookup_order(tenant_id=TENANT, order_id="A-1001")
    second = _connector().lookup_order(tenant_id=TENANT, order_id="A-1001")
    assert first.content() == second.content()
    assert first.digest() == second.digest()
    assert first.source_event_id == second.source_event_id


def test_the_export_is_loaded_as_one_tenant_only() -> None:
    dataset = load_commerce_dataset(SAMPLE)
    assert dataset.tenant_id == TENANT
    with pytest.raises(ContractViolation) as error:
        dataset.require_order("A-1001", tenant_id="tenant-other")
    assert error.value.code is ErrorCode.FORBIDDEN


def test_an_order_outside_the_export_is_refused_not_invented() -> None:
    connector = _connector()
    with pytest.raises(ContractViolation) as error:
        connector.lookup_order(tenant_id=TENANT, order_id="A-9999")
    assert error.value.code is ErrorCode.INVALID_INPUT


def test_the_answer_reports_its_own_source_and_truncates_a_long_history() -> None:

    connector = _connector()
    order = connector.lookup_order(tenant_id=TENANT, order_id="A-1001")
    tracking = connector.lookup_tracking(tenant_id=TENANT, order_id="A-1001")
    assert order.source_id == SOURCES.order_ledger
    assert tracking.source_id == SOURCES.carrier
    shipment = tracking.body["shipment"]
    assert isinstance(shipment, dict)
    events = shipment["events"]
    assert isinstance(events, list)
    assert len(events) <= MAX_TRACKING_EVENTS
    assert shipment["truncated_event_count"] == 0


def test_an_undispatched_order_is_a_fact_not_a_carrier_error() -> None:
    dataset = load_commerce_dataset(SAMPLE)
    stripped = dataset.orders[1].model_copy(update={"shipments": ()})
    empty = CommerceDataset(
        tenant_id=dataset.tenant_id,
        source=dataset.source,
        orders=(dataset.orders[0], stripped),
    )
    answer = CommerceConnector(empty, sources=SOURCES).lookup_tracking(
        tenant_id=TENANT, order_id="A-1002"
    )
    assert answer.body["shipment"] is None
    assert answer.observed_at == stripped.placed_at


def test_the_export_rejects_a_duplicate_order_and_a_broken_timeline() -> None:
    raw = load_commerce_dataset(SAMPLE).model_dump(mode="json")
    duplicate = [*raw["orders"], raw["orders"][0]]
    with pytest.raises(ValidationError):
        CommerceDataset.model_validate({**raw, "orders": duplicate})

    reordered = load_commerce_dataset(SAMPLE).model_dump(mode="json")
    events = reordered["orders"][0]["shipments"][0]["events"]
    events[0]["at"] = events[-1]["at"]
    with pytest.raises(ValidationError):
        CommerceDataset.model_validate(reordered)
