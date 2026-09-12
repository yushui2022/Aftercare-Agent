"""Transactional Inbox/Outbox primitives with explicit replay semantics."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.domain.waits import InboxSignal, check_inbox_replay


@dataclass(frozen=True)
class OutboxDelivery:
    """A leased outbox event handed to a publisher outside a DB transaction.

    ``attempts`` is the delivery fencing token.  It is deliberately returned
    with the row: owner names alone are not sufficient when the same process
    loses a lease and later claims the event again.
    """

    event: DomainEvent
    attempts: int
    owner: str
    lease_until: datetime

    @property
    def tenant_id(self) -> str:
        return self.event.tenant_id

    @property
    def event_id(self) -> str:
        return self.event.event_id


class EventRepository:
    """Persist source signals and application events inside a caller transaction."""

    def append_outbox(self, connection: psycopg.Connection[Any], event: DomainEvent) -> bool:
        inserted = connection.execute(
            "INSERT INTO aftercare_outbox(tenant_id,case_id,event_id,case_seq,event_type,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
            (
                event.tenant_id,
                event.case_id,
                event.event_id,
                event.case_seq,
                event.event_type,
                Jsonb(event.model_dump(mode="json")),
            ),
        ).fetchone()
        if inserted is not None:
            return True
        existing = connection.execute(
            "SELECT event_id,case_id,case_seq,event_type,payload FROM aftercare_outbox "
            "WHERE tenant_id=%s AND (event_id=%s OR (case_id=%s AND case_seq=%s)) FOR UPDATE",
            (event.tenant_id, event.event_id, event.case_id, event.case_seq),
        ).fetchall()
        for row in existing:
            if (
                row[0] == event.event_id
                and DomainEvent.model_validate_json(json.dumps(row[4], ensure_ascii=False)) == event
            ):
                return False
        raise ContractViolation(ErrorCode.CONFLICT, "outbox event identity or sequence conflicts")

    def receive_inbox(self, connection: psycopg.Connection[Any], signal: InboxSignal) -> bool:
        inserted = connection.execute(
            "INSERT INTO aftercare_inbox(tenant_id,case_id,event_id,source_id,source_event_id,"
            "payload,received_at,run_id,wait_id,generation,kind,correlation_key,condition_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
            (
                signal.tenant_id,
                signal.case_id,
                signal.event_id,
                signal.source_id,
                signal.source_event_id,
                Jsonb(signal.model_dump(mode="json")),
                signal.received_at,
                signal.run_id,
                signal.wait_id,
                signal.generation,
                signal.kind,
                signal.correlation_key,
                signal.condition_version,
            ),
        ).fetchone()
        if inserted is not None:
            return True
        row = connection.execute(
            "SELECT payload FROM aftercare_inbox WHERE tenant_id=%s AND source_id=%s "
            "AND source_event_id=%s FOR UPDATE",
            (signal.tenant_id, signal.source_id, signal.source_event_id),
        ).fetchone()
        if row is None:
            # A reused event_id with a different source identity is a hard
            # conflict, not a retry.  This also makes the UNIQUE(event_id)
            # guard observable to callers.
            event_row = connection.execute(
                "SELECT 1 FROM aftercare_inbox WHERE tenant_id=%s AND event_id=%s FOR UPDATE",
                (signal.tenant_id, signal.event_id),
            ).fetchone()
            if event_row is not None:
                raise ContractViolation(ErrorCode.CONFLICT, "inbox event identity conflicts")
            raise ContractViolation(ErrorCode.RETRYABLE, "inbox replay outcome is unknown")
        try:
            stored = InboxSignal.model_validate_json(json.dumps(row[0], ensure_ascii=False))
        except (TypeError, ValueError) as exc:
            raise ContractViolation(ErrorCode.RETRYABLE, "stored inbox signal is invalid") from exc
        check_inbox_replay(stored, signal)
        return False

    def apply_once(
        self,
        connection: psycopg.Connection[Any],
        *,
        tenant_id: str,
        case_id: str,
        consumer_id: str,
        event_id: str,
    ) -> bool:
        inserted = connection.execute(
            "INSERT INTO aftercare_inbox_applications(tenant_id,case_id,consumer_id,event_id) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
            (tenant_id, case_id, consumer_id, event_id),
        ).fetchone()
        return inserted is not None

    def claim_outbox(
        self,
        connection: psycopg.Connection[Any],
        tenant_id: str,
        owner: str,
        lease: timedelta,
        *,
        limit: int = 1,
    ) -> tuple[OutboxDelivery, ...]:
        """Claim due events and return them for publishing after commit.

        The caller must commit this short transaction before invoking a
        network publisher.  ``SKIP LOCKED`` lets multiple publisher instances
        share a tenant without waiting on one another.  Expired claims are
        reclaimed and increment ``delivery_attempts``, which fences an old
        in-flight acknowledgement even when owner strings happen to match.
        """
        if not owner or not isinstance(limit, int) or limit < 1:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "outbox owner and positive limit required"
            )
        if lease <= timedelta(0):
            raise ContractViolation(ErrorCode.INVALID_INPUT, "outbox lease must be positive")
        seconds = lease.total_seconds()
        rows = connection.execute(
            "WITH candidates AS ("
            " SELECT tenant_id,event_id FROM aftercare_outbox"
            " WHERE tenant_id=%s AND ((delivery_state='PENDING' AND "
            "next_attempt_at <= clock_timestamp())"
            " OR (delivery_state='CLAIMED' AND delivery_lease_until <= clock_timestamp()))"
            " ORDER BY next_attempt_at, recorded_at, event_id"
            " FOR UPDATE SKIP LOCKED LIMIT %s"
            ")"
            " UPDATE aftercare_outbox AS o"
            " SET delivery_state='CLAIMED', delivery_owner=%s,"
            " delivery_lease_until=clock_timestamp() + (%s * interval '1 second'),"
            " delivery_attempts=o.delivery_attempts + 1, last_error=NULL"
            " FROM candidates AS c"
            " WHERE o.tenant_id=c.tenant_id AND o.event_id=c.event_id"
            " RETURNING o.payload,o.delivery_attempts,o.delivery_owner,o.delivery_lease_until",
            (tenant_id, limit, owner, seconds),
        ).fetchall()
        deliveries: list[OutboxDelivery] = []
        for payload, attempts, claimed_owner, lease_until in rows:
            try:
                event = DomainEvent.model_validate_json(json.dumps(payload, ensure_ascii=False))
            except (TypeError, ValueError) as exc:
                raise ContractViolation(
                    ErrorCode.RETRYABLE, "stored outbox event is invalid"
                ) from exc
            if not isinstance(claimed_owner, str) or not isinstance(lease_until, datetime):
                raise ContractViolation(ErrorCode.RETRYABLE, "stored outbox lease is invalid")
            deliveries.append(
                OutboxDelivery(
                    event=event,
                    attempts=int(attempts),
                    owner=claimed_owner,
                    lease_until=lease_until,
                )
            )
        return tuple(deliveries)

    def claim_outbox_batch(
        self,
        connection: psycopg.Connection[Any],
        tenant_id: str,
        owner: str,
        lease: timedelta,
        *,
        limit: int = 100,
    ) -> tuple[OutboxDelivery, ...]:
        """Named batch variant for callers that want an explicit batch API."""
        return self.claim_outbox(connection, tenant_id, owner, lease, limit=limit)

    def ack_outbox(
        self,
        connection: psycopg.Connection[Any],
        delivery: OutboxDelivery,
        *,
        delivered_at: datetime | None = None,
    ) -> bool:
        """Mark a leased event as delivered, fenced by its attempt token."""
        when = delivered_at or datetime.now(UTC)
        if when.tzinfo is None or when.utcoffset() is None:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "delivered_at must be timezone-aware")
        updated = connection.execute(
            "UPDATE aftercare_outbox SET delivery_state='ACKED', delivery_owner=NULL,"
            "delivery_lease_until=NULL, delivered_at=%s, last_error=NULL "
            "WHERE tenant_id=%s AND event_id=%s AND delivery_state='CLAIMED'"
            " AND delivery_owner=%s AND delivery_attempts=%s"
            " AND delivery_lease_until > clock_timestamp() RETURNING 1",
            (when, delivery.tenant_id, delivery.event_id, delivery.owner, delivery.attempts),
        ).fetchone()
        if updated is not None:
            return True
        row = connection.execute(
            "SELECT delivery_state,delivery_owner,delivery_attempts,delivery_lease_until "
            "FROM aftercare_outbox WHERE tenant_id=%s AND event_id=%s FOR UPDATE",
            (delivery.tenant_id, delivery.event_id),
        ).fetchone()
        if row is not None and row[0] == "ACKED":
            return False
        raise ContractViolation(ErrorCode.LEASE_LOST, "outbox acknowledgement lease was lost")

    def retry_outbox(
        self,
        connection: psycopg.Connection[Any],
        delivery: OutboxDelivery,
        *,
        error: str,
        delay: timedelta = timedelta(seconds=5),
    ) -> bool:
        """Release a failed delivery for a later attempt.

        This does not acknowledge the event.  A subsequent claim can publish
        it again, so downstream consumers must retain their inbox/idempotency
        protections.  Error text is bounded; the caller must pass a sanitized
        error code, never a provider response or secret-bearing traceback.
        """
        if delay < timedelta(0):
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "outbox retry delay cannot be negative"
            )
        message = str(error)[:2000]
        updated = connection.execute(
            "UPDATE aftercare_outbox SET delivery_state='PENDING', delivery_owner=NULL,"
            "delivery_lease_until=NULL, next_attempt_at=clock_timestamp() + "
            "(%s * interval '1 second'),"
            "last_error=%s WHERE tenant_id=%s AND event_id=%s AND delivery_state='CLAIMED'"
            " AND delivery_owner=%s AND delivery_attempts=%s"
            " AND delivery_lease_until > clock_timestamp() RETURNING 1",
            (
                delay.total_seconds(),
                message,
                delivery.tenant_id,
                delivery.event_id,
                delivery.owner,
                delivery.attempts,
            ),
        ).fetchone()
        if updated is not None:
            return True
        row = connection.execute(
            "SELECT delivery_state FROM aftercare_outbox "
            "WHERE tenant_id=%s AND event_id=%s FOR UPDATE",
            (delivery.tenant_id, delivery.event_id),
        ).fetchone()
        if row is not None and row[0] == "ACKED":
            raise ContractViolation(ErrorCode.CONFLICT, "cannot retry an acknowledged outbox event")
        raise ContractViolation(ErrorCode.LEASE_LOST, "outbox retry lease was lost")
