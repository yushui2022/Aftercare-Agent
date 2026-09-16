"""An imported, tenant-scoped commerce export in a benchmark-shaped form.

The shape follows what a public commerce-agent benchmark asks an agent to
reason about -- an order with line items and a payment, shipments with
tracking events, and buyer messages -- without copying any dataset, brand
asset or recorded provider response.  A deployment imports its own export
here.  Nothing in this module talks to a network or a database, and every
lookup is tenant-scoped by construction rather than by caller discipline.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from aftercare_agent.domain.common import (
    ContractModel,
    ContractViolation,
    ErrorCode,
    Identifier,
    PositiveInt,
    SchemaVersion,
    UtcDatetime,
)
from aftercare_agent.domain.investigation import BuyerAssertion

type Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
type MinorAmount = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$")]
type ShortText = Annotated[str, Field(min_length=1, max_length=200)]
type LongText = Annotated[str, Field(min_length=1, max_length=2000)]


class Money(ContractModel):
    """Integer minor units; no floating-point money anywhere in the export."""

    amount_minor: MinorAmount
    currency: Currency


class LineItem(ContractModel):
    sku: Identifier
    title: ShortText
    quantity: PositiveInt
    unit_price: Money


class Payment(ContractModel):
    payment_id: Identifier
    method: Identifier
    amount: Money
    status: Literal["captured", "refunded", "partially_refunded", "pending"]
    captured_at: UtcDatetime


class TrackingEvent(ContractModel):
    at: UtcDatetime
    code: Identifier
    location: ShortText
    description: ShortText


class Shipment(ContractModel):
    shipment_id: Identifier
    carrier: Identifier
    tracking_number: Identifier
    dispatched_at: UtcDatetime
    delivered_at: UtcDatetime | None = None
    events: Annotated[tuple[TrackingEvent, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def chronological(self) -> Self:
        stamps = [self.dispatched_at, *(event.at for event in self.events)]
        if stamps != sorted(stamps):
            raise ValueError("shipment events must be chronological")
        if self.delivered_at is not None and self.delivered_at < stamps[-1]:
            raise ValueError("delivery cannot precede the last tracking event")
        return self


class BuyerMessage(ContractModel):
    message_id: Identifier
    channel: Identifier
    received_at: UtcDatetime
    assertion: BuyerAssertion
    text: LongText


class Order(ContractModel):
    order_id: Identifier
    buyer_id: Identifier
    placed_at: UtcDatetime
    status: Literal["placed", "shipped", "delivered", "cancelled", "refunded"]
    items: Annotated[tuple[LineItem, ...], Field(min_length=1, max_length=64)]
    payment: Payment
    shipments: Annotated[tuple[Shipment, ...], Field(max_length=16)] = ()
    messages: Annotated[tuple[BuyerMessage, ...], Field(max_length=64)] = ()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if len({item.sku for item in self.items}) != len(self.items):
            raise ValueError("line item SKUs must be unique per order")
        if len({shipment.shipment_id for shipment in self.shipments}) != len(self.shipments):
            raise ValueError("shipment ids must be unique per order")
        if len({message.message_id for message in self.messages}) != len(self.messages):
            raise ValueError("message ids must be unique per order")
        if self.payment.captured_at < self.placed_at:
            raise ValueError("payment cannot precede the order")
        if any(shipment.dispatched_at < self.placed_at for shipment in self.shipments):
            raise ValueError("dispatch cannot precede the order")
        currency = self.payment.amount.currency
        if any(item.unit_price.currency != currency for item in self.items):
            raise ValueError("line items must share the order currency")
        return self


class CommerceDataset(ContractModel):
    """One tenant's imported export; authenticated scope is checked on every lookup."""

    schema_version: SchemaVersion = 1
    tenant_id: Identifier
    source: ShortText
    orders: Annotated[tuple[Order, ...], Field(min_length=1, max_length=10_000)]

    @model_validator(mode="after")
    def unique_orders(self) -> Self:
        if len({order.order_id for order in self.orders}) != len(self.orders):
            raise ValueError("order ids must be unique in one dataset")
        return self

    def digest(self) -> str:
        """Stable digest of the imported facts, recorded with every answer."""
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    def find(self, order_id: str) -> Order | None:
        for order in self.orders:
            if order.order_id == order_id:
                return order
        return None

    def require_order(self, order_id: str, *, tenant_id: str) -> Order:
        if tenant_id != self.tenant_id:
            # One dataset never answers for another tenant, whatever the caller
            # passes: scope comes from the authenticated Run, not the tool call.
            raise ContractViolation(ErrorCode.FORBIDDEN, "dataset belongs to another tenant")
        order = self.find(order_id)
        if order is None:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "order is not in this tenant export")
        return order


def load_commerce_dataset(path: str | Path) -> CommerceDataset:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "commerce export is not readable") from exc
    try:
        return CommerceDataset.model_validate_json(raw)
    except ValidationError as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "commerce export is invalid") from exc
