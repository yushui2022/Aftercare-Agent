"""Transactional Inbox/Outbox primitives with explicit replay semantics."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.events import DomainEvent, DomainEventDraft
from aftercare_agent.domain.waits import InboxSignal, check_inbox_replay

MAX_OUTBOX_BATCH = 500


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

    @staticmethod
    def _lock_case_sequence(
        connection: psycopg.Connection[Any], tenant_id: str, case_id: str
    ) -> int:
        """Lock and return a case-local high-water mark.

        This is deliberately a row lock, rather than a PostgreSQL sequence:
        aborted transactions do not consume visible case positions and two
        workers cannot allocate the same position.  The counter is also
        reconciled with legacy explicit ``case_seq`` writes below.
        """
        # Production case writers already have a Case row.  Lock it first so
        # event allocation composes with Case→Run→Wait/Action gate paths.  A
        # few import/backfill callers intentionally write events before the
        # case exists; those retain the counter-only compatibility path.
        connection.execute(
            "SELECT 1 FROM aftercare_cases WHERE tenant_id=%s AND case_id=%s FOR UPDATE",
            (tenant_id, case_id),
        )
        connection.execute(
            "INSERT INTO aftercare_case_event_sequences(tenant_id,case_id,last_case_seq) "
            "SELECT %s,%s,COALESCE(MAX(case_seq),0) FROM aftercare_outbox "
            "WHERE tenant_id=%s AND case_id=%s ON CONFLICT DO NOTHING",
            (tenant_id, case_id, tenant_id, case_id),
        )
        row = connection.execute(
            "SELECT last_case_seq FROM aftercare_case_event_sequences "
            "WHERE tenant_id=%s AND case_id=%s FOR UPDATE",
            (tenant_id, case_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "case event sequence row is unavailable")
        return int(row[0])

    @staticmethod
    def _snapshot_sha256(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def append_event(
        self,
        connection: psycopg.Connection[Any],
        draft: DomainEventDraft,
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> tuple[DomainEvent, bool]:
        """Allocate the next case position and append one immutable event.

        ``snapshot`` is optional for compatibility, but gate events should
        provide it: the outbox stores only a content-addressed artifact
        reference while this table preserves the exact decision/request that
        was true at the transition.  Replays return the original event and
        never advance the counter.
        """
        current = self._lock_case_sequence(connection, draft.tenant_id, draft.case_id)
        existing = connection.execute(
            "SELECT payload FROM aftercare_outbox WHERE tenant_id=%s AND event_id=%s FOR UPDATE",
            (draft.tenant_id, draft.event_id),
        ).fetchone()
        if existing is not None:
            try:
                event = DomainEvent.model_validate_json(json.dumps(existing[0], ensure_ascii=False))
            except (TypeError, ValueError) as exc:
                raise ContractViolation(
                    ErrorCode.RETRYABLE, "stored outbox event is invalid"
                ) from exc
            if event != DomainEvent.model_validate(
                draft.model_dump() | {"case_seq": event.case_seq}
            ):
                raise ContractViolation(ErrorCode.CONFLICT, "outbox event identity conflicts")
            return event, True
        if snapshot is not None:
            digest = self._snapshot_sha256(snapshot)
            if digest != draft.payload.sha256:
                raise ContractViolation(ErrorCode.CONFLICT, "event snapshot digest does not match")
            payload_row = connection.execute(
                "SELECT sha256,payload FROM aftercare_event_payloads "
                "WHERE tenant_id=%s AND case_id=%s AND reference_id=%s FOR UPDATE",
                (draft.tenant_id, draft.case_id, draft.payload.reference_id),
            ).fetchone()
            if payload_row is not None and (
                payload_row[0] != draft.payload.sha256 or payload_row[1] != snapshot
            ):
                raise ContractViolation(ErrorCode.CONFLICT, "event payload snapshot conflicts")
            connection.execute(
                "INSERT INTO aftercare_event_payloads(tenant_id,case_id,reference_id,sha256,"
                "payload) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    draft.tenant_id,
                    draft.case_id,
                    draft.payload.reference_id,
                    draft.payload.sha256,
                    Jsonb(snapshot),
                ),
            )
        event = DomainEvent.model_validate(draft.model_dump() | {"case_seq": current + 1})
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
        if inserted is None:
            # A concurrent legacy writer may have occupied the position.  The
            # case counter is locked, so only a cross-transaction explicit
            # write can produce this path; surface conflict rather than guess.
            raise ContractViolation(ErrorCode.CONFLICT, "case event sequence was occupied")
        connection.execute(
            "UPDATE aftercare_case_event_sequences SET last_case_seq=%s "
            "WHERE tenant_id=%s AND case_id=%s",
            (event.case_seq, event.tenant_id, event.case_id),
        )
        return event, False

    def append_outbox(self, connection: psycopg.Connection[Any], event: DomainEvent) -> bool:
        current = self._lock_case_sequence(connection, event.tenant_id, event.case_id)
        # Resolve an idempotent replay before enforcing the next position. A
        # retry of an old event necessarily has ``case_seq <= current`` and
        # must remain a no-op rather than being rejected as a new gap.
        existing_by_id = connection.execute(
            "SELECT payload FROM aftercare_outbox WHERE tenant_id=%s AND event_id=%s FOR UPDATE",
            (event.tenant_id, event.event_id),
        ).fetchone()
        if existing_by_id is not None:
            try:
                stored = DomainEvent.model_validate_json(
                    json.dumps(existing_by_id[0], ensure_ascii=False)
                )
            except (TypeError, ValueError) as exc:
                raise ContractViolation(
                    ErrorCode.RETRYABLE, "stored outbox event is invalid"
                ) from exc
            if stored == event:
                return False
            raise ContractViolation(ErrorCode.CONFLICT, "outbox event identity conflicts")
        if event.case_seq != current + 1:
            raise ContractViolation(
                ErrorCode.CONFLICT,
                "outbox case sequence must be the next contiguous position",
            )
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
            if event.case_seq > current:
                connection.execute(
                    "UPDATE aftercare_case_event_sequences SET last_case_seq=%s "
                    "WHERE tenant_id=%s AND case_id=%s",
                    (event.case_seq, event.tenant_id, event.case_id),
                )
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

    def list_case_events(
        self,
        connection: psycopg.Connection[Any],
        *,
        tenant_id: str,
        case_id: str,
        after_case_seq: int = 0,
        limit: int = 100,
    ) -> tuple[DomainEvent, ...]:
        """Read a bounded, case-scoped event page for replay/SSE consumers."""
        if type(after_case_seq) is not int or after_case_seq < 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "after_case_seq must be non-negative")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "event limit must be between 1 and 500"
            )
        rows = connection.execute(
            "SELECT payload FROM aftercare_outbox WHERE tenant_id=%s AND case_id=%s "
            "AND case_seq>%s ORDER BY case_seq LIMIT %s",
            (tenant_id, case_id, after_case_seq, limit),
        ).fetchall()
        events: list[DomainEvent] = []
        for row in rows:
            try:
                events.append(
                    DomainEvent.model_validate_json(json.dumps(row[0], ensure_ascii=False))
                )
            except (TypeError, ValueError) as exc:
                raise ContractViolation(
                    ErrorCode.RETRYABLE, "stored case event is invalid"
                ) from exc
        return tuple(events)

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
        if not owner or type(limit) is not int or not 1 <= limit <= MAX_OUTBOX_BATCH:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT,
                f"outbox owner and limit between 1 and {MAX_OUTBOX_BATCH} required",
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
