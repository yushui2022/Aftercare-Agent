"""Read-only commerce lookups shaped for the investigation tools.

The connector answers from an imported export and nothing else: it is not a
network client, it cannot write, and the source identity it reports is
deployment-owned, so a model can neither choose nor spoof where a fact came
from.  Answers are canonical JSON with a digest, and they contain no value
derived from the wall clock -- a replayed step has to produce byte-identical
evidence, or a retry would look like a second, different fact.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from aftercare_agent.domain.common import (
    ContractModel,
    ContractViolation,
    ErrorCode,
    Identifier,
    utc,
)

from .dataset import BuyerMessage, CommerceDataset, Money, Shipment, TrackingEvent

ORDER_FACTS_SCHEMA = "aftercare.commerce.order.v1"
TRACKING_FACTS_SCHEMA = "aftercare.commerce.tracking.v1"
BUYER_FACTS_SCHEMA = "aftercare.commerce.buyer.v1"
# Bounded on purpose: the model sees the tail of a long delivery history, not
# an export of it, so one answer cannot quietly become the whole context.
MAX_TRACKING_EVENTS = 10
MAX_BUYER_MESSAGE_PAGE_SIZE = 16


class CommerceSources(ContractModel):
    """Source identities owned by the deployment, never by a model request."""

    order_ledger: Identifier
    carrier: Identifier
    buyer_channel: Identifier


@dataclass(frozen=True)
class ConnectorAnswer:
    """One canonical answer, addressed by its own digest."""

    tool: str
    source_id: str
    source_event_id: str
    observed_at: datetime
    body: Mapping[str, object]

    def content(self) -> bytes:
        return json.dumps(
            self.body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def digest(self) -> str:
        return sha256(self.content()).hexdigest()


@dataclass(frozen=True)
class BuyerMessagePage:
    """A deterministic, host-only page over one order's buyer messages."""

    items: tuple[ConnectorAnswer, ...]
    last_message_id: str | None
    next_cursor: str | None


def _iso(value: datetime) -> str:
    return utc(value).isoformat()


def _money(value: Money) -> dict[str, object]:
    return {"amount_minor": value.amount_minor, "currency": value.currency}


def _event_id(prefix: str, payload: object) -> str:
    """Identifier-safe, deterministic event id: an ISO stamp is not one."""
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return f"{prefix}:{sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def _event_payload(event: TrackingEvent) -> dict[str, object]:
    return {
        "at": _iso(event.at),
        "code": event.code,
        "location": event.location,
        "description": event.description,
    }


def _shipment_section(shipment: Shipment) -> dict[str, object]:
    tail = shipment.events[-MAX_TRACKING_EVENTS:]
    return {
        "shipment_id": shipment.shipment_id,
        "carrier": shipment.carrier,
        "tracking_number": shipment.tracking_number,
        "dispatched_at": _iso(shipment.dispatched_at),
        "delivered_at": None if shipment.delivered_at is None else _iso(shipment.delivered_at),
        "latest_event": _event_payload(shipment.events[-1]),
        "events": [_event_payload(event) for event in tail],
        "truncated_event_count": len(shipment.events) - len(tail),
    }


class CommerceConnector:
    """Deterministic lookups over one imported export."""

    def __init__(self, dataset: CommerceDataset, *, sources: CommerceSources) -> None:
        self._dataset = dataset
        self._sources = sources

    @property
    def dataset_digest(self) -> str:
        return self._dataset.digest()

    @property
    def tenant_id(self) -> str:
        return self._dataset.tenant_id

    def lookup_order(self, *, tenant_id: str, order_id: str) -> ConnectorAnswer:
        order = self._dataset.require_order(order_id, tenant_id=tenant_id)
        body: dict[str, object] = {
            "schema": ORDER_FACTS_SCHEMA,
            "dataset_digest": self._dataset.digest(),
            "order_id": order.order_id,
            "buyer_id": order.buyer_id,
            "placed_at": _iso(order.placed_at),
            "status": order.status,
            "items": [
                {
                    "sku": item.sku,
                    "title": item.title,
                    "quantity": item.quantity,
                    "unit_price": _money(item.unit_price),
                }
                for item in order.items
            ],
            "payment": {
                "payment_id": order.payment.payment_id,
                "method": order.payment.method,
                "status": order.payment.status,
                "amount": _money(order.payment.amount),
                "captured_at": _iso(order.payment.captured_at),
            },
            "shipment_count": len(order.shipments),
            "message_count": len(order.messages),
        }
        return ConnectorAnswer(
            tool="lookup_order",
            source_id=self._sources.order_ledger,
            # The source record's own version, not this rendering of it: two
            # different tools asking about one unchanged order must agree.
            source_event_id=_event_id(
                "order",
                {
                    "order_id": order.order_id,
                    "status": order.status,
                    "placed_at": _iso(order.placed_at),
                    "payment_status": order.payment.status,
                    "captured_at": _iso(order.payment.captured_at),
                },
            ),
            # The newest fact in the export, not the time of asking.
            observed_at=max(order.placed_at, order.payment.captured_at),
            body=body,
        )

    def lookup_tracking(self, *, tenant_id: str, order_id: str) -> ConnectorAnswer:
        order = self._dataset.require_order(order_id, tenant_id=tenant_id)
        shipment = order.shipments[-1] if order.shipments else None
        body: dict[str, object] = {
            "schema": TRACKING_FACTS_SCHEMA,
            "dataset_digest": self._dataset.digest(),
            "order_id": order.order_id,
            "carrier": None if shipment is None else shipment.carrier,
            "tracking_number": None if shipment is None else shipment.tracking_number,
            "shipment": None if shipment is None else _shipment_section(shipment),
            "order_status": order.status,
        }
        if shipment is None:
            # No dispatch is a fact about this order, not a carrier failure.
            return ConnectorAnswer(
                tool="lookup_tracking",
                source_id=self._sources.carrier,
                source_event_id=_event_id("shipment:none", body),
                observed_at=order.placed_at,
                body=body,
            )
        return ConnectorAnswer(
            tool="lookup_tracking",
            source_id=self._sources.carrier,
            source_event_id=_event_id("shipment", _shipment_section(shipment)),
            observed_at=(
                shipment.events[-1].at
                if shipment.delivered_at is None
                else max(shipment.delivered_at, shipment.events[-1].at)
            ),
            body=body,
        )

    def lookup_buyer_message(self, *, tenant_id: str, order_id: str) -> ConnectorAnswer:
        """Return the newest buyer statement without letting the caller pick one.

        A later slice can add a cursor for a long conversation.  This bounded
        adapter deliberately exposes one deterministic message per call so a
        model cannot select a convenient historical statement or invent a
        message identifier.
        """
        order = self._dataset.require_order(order_id, tenant_id=tenant_id)
        message = max(
            order.messages, key=lambda item: (item.received_at, item.message_id), default=None
        )
        body: dict[str, object] = {
            "schema": BUYER_FACTS_SCHEMA,
            "dataset_digest": self._dataset.digest(),
            "order_id": order.order_id,
            "message": None,
        }
        if message is not None:
            return self._buyer_answer(order_id=order.order_id, message=message)
        return ConnectorAnswer(
            tool="lookup_buyer_message",
            source_id=self._sources.buyer_channel,
            source_event_id=_event_id("buyer-message:none", body),
            observed_at=order.placed_at,
            body=body,
        )

    def lookup_buyer_messages(
        self,
        *,
        tenant_id: str,
        order_id: str,
        after_message_id: str | None = None,
        limit: int = MAX_BUYER_MESSAGE_PAGE_SIZE,
    ) -> BuyerMessagePage:
        """Read a stable chronological page for a trusted host.

        The cursor is a message ID from this same export and is deliberately
        not exposed as a model-controlled tool argument.  Ordering ties by
        message ID, so retries and page boundaries remain deterministic.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_BUYER_MESSAGE_PAGE_SIZE:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "buyer message page limit is out of range"
            )
        order = self._dataset.require_order(order_id, tenant_id=tenant_id)
        messages = tuple(
            sorted(order.messages, key=lambda item: (item.received_at, item.message_id))
        )
        start = 0
        if after_message_id is not None:
            if not after_message_id or after_message_id not in {
                item.message_id for item in messages
            }:
                raise ContractViolation(ErrorCode.INVALID_INPUT, "buyer message cursor is unknown")
            start = (
                next(
                    index
                    for index, item in enumerate(messages)
                    if item.message_id == after_message_id
                )
                + 1
            )
        selected = messages[start : start + limit]
        answers = tuple(
            self._buyer_answer(order_id=order.order_id, message=message) for message in selected
        )
        last_message_id = selected[-1].message_id if selected else None
        next_cursor = last_message_id if start + len(selected) < len(messages) else None
        return BuyerMessagePage(
            items=answers,
            last_message_id=last_message_id,
            next_cursor=next_cursor,
        )

    def _buyer_answer(self, *, order_id: str, message: BuyerMessage) -> ConnectorAnswer:
        """Render one trusted BuyerMessage as the existing evidence answer."""
        # The dataset validator guarantees this shape; keeping the helper
        # private prevents callers from injecting a message object.
        body: dict[str, object] = {
            "schema": BUYER_FACTS_SCHEMA,
            "dataset_digest": self._dataset.digest(),
            "order_id": order_id,
            "message": {
                "message_id": message.message_id,
                "channel": message.channel,
                "received_at": _iso(message.received_at),
                "assertion": message.assertion,
                "text": message.text,
            },
        }
        return ConnectorAnswer(
            tool="lookup_buyer_message",
            source_id=self._sources.buyer_channel,
            source_event_id=_event_id("buyer-message", body["message"]),
            observed_at=message.received_at,
            body=body,
        )

    def registered_sources(self) -> Mapping[str, str]:
        """The source identities this connector is allowed to speak for."""
        return {
            "lookup_order": self._sources.order_ledger,
            "lookup_tracking": self._sources.carrier,
            "lookup_buyer_message": self._sources.buyer_channel,
        }


__all__ = [
    "CommerceConnector",
    "CommerceSources",
    "ConnectorAnswer",
    "BUYER_FACTS_SCHEMA",
    "BuyerMessagePage",
    "MAX_BUYER_MESSAGE_PAGE_SIZE",
    "MAX_TRACKING_EVENTS",
    "ORDER_FACTS_SCHEMA",
    "TRACKING_FACTS_SCHEMA",
]
