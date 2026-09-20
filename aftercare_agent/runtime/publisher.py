"""Short-transaction Outbox publisher loop and deterministic test sink."""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol

from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.observability import Tracer
from aftercare_agent.persistence import Database, EventRepository


class EventPublisher(Protocol):
    """Network boundary; implementations must be safe to call outside SQL."""

    def publish(self, event: DomainEvent) -> None:
        """Publish one event or raise an exception that can be retried."""


@dataclass(frozen=True)
class PublishResult:
    claimed: int
    acknowledged: int
    retried: int


class OutboxPublisher:
    """Drain a bounded batch without holding a database transaction over I/O."""

    def __init__(
        self,
        database: Database,
        *,
        events: EventRepository | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.database = database
        self.events = events or EventRepository()
        self.tracer = tracer

    def publish_once(
        self,
        *,
        tenant_id: str,
        owner: str,
        publisher: EventPublisher,
        lease: timedelta = timedelta(seconds=30),
        limit: int = 100,
        retry_delay: timedelta = timedelta(seconds=5),
    ) -> PublishResult:
        # Claim and commit before invoking a broker/client.  A crash after
        # publish but before ack is intentionally a duplicate-delivery case;
        # inbox/idempotency at the consumer is the correctness boundary.
        with self.database.transaction(tenant_id=tenant_id, subject_id=owner) as connection:
            deliveries = self.events.claim_outbox(connection, tenant_id, owner, lease, limit=limit)
        acknowledged = 0
        retried = 0
        for delivery in deliveries:
            try:
                if self.tracer is None:
                    publisher.publish(delivery.event)
                else:
                    with self.tracer.span(
                        "aftercare.outbox.publish",
                        {
                            "tenant_id": delivery.tenant_id,
                            "event_type": delivery.event.event_type,
                        },
                    ):
                        publisher.publish(delivery.event)
            except Exception as exc:
                with self.database.transaction(
                    tenant_id=delivery.tenant_id, subject_id=owner
                ) as connection:
                    self.events.retry_outbox(
                        connection, delivery, error=type(exc).__name__, delay=retry_delay
                    )
                retried += 1
            else:
                with self.database.transaction(
                    tenant_id=delivery.tenant_id, subject_id=owner
                ) as connection:
                    self.events.ack_outbox(connection, delivery)
                acknowledged += 1
        return PublishResult(claimed=len(deliveries), acknowledged=acknowledged, retried=retried)


@dataclass
class FakePublisher:
    """In-memory sink used by integration tests and local demonstrations."""

    fail_first: int = 0
    events: list[DomainEvent] = field(default_factory=list)

    def publish(self, event: DomainEvent) -> None:
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("synthetic publisher failure")
        self.events.append(event)
