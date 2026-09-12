"""Durable, case-ordered consumer projection bookkeeping.

The repository is deliberately transport agnostic.  A consumer can feed it
events from Outbox, Kafka, NATS, or a replay job; the same short transaction
persists a future event in the gap buffer or advances the contiguous position.
It does not execute user callbacks or publish SSE while holding a database
lock.  Callers that update a materialized view should do that update in the
same transaction before/after :meth:`ingest`.
"""

import json
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.events import (
    DomainEvent,
    ProjectionPosition,
    projection_decision,
)

ProjectionDecision = Literal["apply", "buffer_gap", "replay"]


@dataclass(frozen=True)
class ProjectionIngestResult:
    """Outcome of an idempotent ingest and any contiguous drain."""

    decision: ProjectionDecision
    applied: tuple[DomainEvent, ...]
    position: ProjectionPosition


_POSITION_FIELDS = ("tenant_id", "case_id", "consumer_id", "last_case_seq")
_EVENT_FIELDS = ("case_seq", "event_id", "payload")


def _decode(raw: Any, *, what: str) -> DomainEvent:
    try:
        return DomainEvent.model_validate_json(json.dumps(raw, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(ErrorCode.RETRYABLE, f"stored {what} event is invalid") from exc


class ProjectionRepository:
    """Persist a per-consumer case position and an ordered event ledger.

    Every public method expects a caller-owned transaction.  The position row
    is the serialization point for one ``(tenant, case, consumer)`` stream;
    no process-wide lock is taken, so unrelated cases and consumers proceed
    independently.
    """

    def _ensure_position(
        self,
        connection: psycopg.Connection[Any],
        tenant_id: str,
        case_id: str,
        consumer_id: str,
    ) -> ProjectionPosition:
        connection.execute(
            "INSERT INTO aftercare_projection_positions"
            "(tenant_id,case_id,consumer_id,last_case_seq) VALUES (%s,%s,%s,0)"
            " ON CONFLICT DO NOTHING",
            (tenant_id, case_id, consumer_id),
        )
        row = connection.execute(
            "SELECT tenant_id,case_id,consumer_id,last_case_seq "
            "FROM aftercare_projection_positions "
            "WHERE tenant_id=%s AND case_id=%s AND consumer_id=%s FOR UPDATE",
            (tenant_id, case_id, consumer_id),
        ).fetchone()
        if row is None:
            raise ContractViolation(ErrorCode.RETRYABLE, "projection position disappeared")
        return ProjectionPosition.model_validate(dict(zip(_POSITION_FIELDS, row, strict=False)))

    @staticmethod
    def _same_event(stored: DomainEvent, incoming: DomainEvent) -> bool:
        return stored == incoming

    def _find_applied_by_id(
        self,
        connection: psycopg.Connection[Any],
        tenant_id: str,
        case_id: str,
        consumer_id: str,
        event_id: str,
    ) -> tuple[int, DomainEvent] | None:
        row = connection.execute(
            "SELECT case_seq,payload FROM aftercare_projection_applied "
            "WHERE tenant_id=%s AND case_id=%s AND consumer_id=%s AND event_id=%s FOR UPDATE",
            (tenant_id, case_id, consumer_id, event_id),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), _decode(row[1], what="applied")

    def _find_buffered_by_id(
        self,
        connection: psycopg.Connection[Any],
        tenant_id: str,
        case_id: str,
        consumer_id: str,
        event_id: str,
    ) -> tuple[int, DomainEvent] | None:
        row = connection.execute(
            "SELECT case_seq,payload FROM aftercare_projection_buffer "
            "WHERE tenant_id=%s AND case_id=%s AND consumer_id=%s AND event_id=%s FOR UPDATE",
            (tenant_id, case_id, consumer_id, event_id),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), _decode(row[1], what="buffered")

    def _event_at_sequence(
        self,
        connection: psycopg.Connection[Any],
        table: Literal["aftercare_projection_applied", "aftercare_projection_buffer"],
        tenant_id: str,
        case_id: str,
        consumer_id: str,
        case_seq: int,
    ) -> tuple[str, DomainEvent] | None:
        row = connection.execute(
            f"SELECT event_id,payload FROM {table} "
            "WHERE tenant_id=%s AND case_id=%s AND consumer_id=%s AND case_seq=%s FOR UPDATE",
            (tenant_id, case_id, consumer_id, case_seq),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), _decode(row[1], what="projection")

    def _record_applied(
        self,
        connection: psycopg.Connection[Any],
        event: DomainEvent,
        consumer_id: str,
    ) -> None:
        connection.execute(
            "INSERT INTO aftercare_projection_applied"
            "(tenant_id,case_id,consumer_id,case_seq,event_id,payload) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                event.tenant_id,
                event.case_id,
                consumer_id,
                event.case_seq,
                event.event_id,
                Jsonb(event.model_dump(mode="json")),
            ),
        )

    def _drain(
        self,
        connection: psycopg.Connection[Any],
        position: ProjectionPosition,
    ) -> tuple[ProjectionPosition, tuple[DomainEvent, ...]]:
        """Move every now-contiguous buffered event into the applied ledger.

        The position is already locked by ``_ensure_position``.  Buffer rows
        are selected one at a time in sequence order; a concurrent ingest for
        the same consumer therefore cannot create a duplicate or skip a gap.
        """

        applied: list[DomainEvent] = []
        current = position
        while True:
            buffered = self._event_at_sequence(
                connection,
                "aftercare_projection_buffer",
                current.tenant_id,
                current.case_id,
                current.consumer_id,
                current.last_case_seq + 1,
            )
            if buffered is None:
                break
            _, event = buffered
            if event.case_seq != current.last_case_seq + 1:
                raise ContractViolation(ErrorCode.CONFLICT, "buffer sequence is inconsistent")
            self._record_applied(connection, event, current.consumer_id)
            connection.execute(
                "DELETE FROM aftercare_projection_buffer WHERE tenant_id=%s AND case_id=%s "
                "AND consumer_id=%s AND case_seq=%s",
                (current.tenant_id, current.case_id, current.consumer_id, event.case_seq),
            )
            current = current.model_copy(update={"last_case_seq": event.case_seq})
            connection.execute(
                "UPDATE aftercare_projection_positions SET last_case_seq=%s,"
                "updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND case_id=%s AND consumer_id=%s",
                (
                    current.last_case_seq,
                    current.tenant_id,
                    current.case_id,
                    current.consumer_id,
                ),
            )
            applied.append(event)
        return current, tuple(applied)

    def ingest(
        self,
        connection: psycopg.Connection[Any],
        event: DomainEvent,
        *,
        consumer_id: str,
    ) -> ProjectionIngestResult:
        """Accept one event and drain newly contiguous events atomically.

        ``buffer_gap`` means the event was retained, not acknowledged as
        applied.  A duplicate of a retained row is idempotent and returns
        ``buffer_gap`` with no new row.  A duplicate of an applied row returns
        ``replay``.  Any event-id or sequence collision with different full
        content raises ``CONFLICT``.
        """

        if not consumer_id:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "consumer_id is required")
        position = self._ensure_position(connection, event.tenant_id, event.case_id, consumer_id)

        applied_by_id = self._find_applied_by_id(
            connection, event.tenant_id, event.case_id, consumer_id, event.event_id
        )
        if applied_by_id is not None:
            sequence, stored = applied_by_id
            if sequence != event.case_seq or not self._same_event(stored, event):
                raise ContractViolation(ErrorCode.CONFLICT, "applied event identity conflicts")
            if position.last_case_seq < event.case_seq:
                raise ContractViolation(
                    ErrorCode.CONFLICT, "projection position trails applied index"
                )
            return ProjectionIngestResult("replay", (), position)

        buffered_by_id = self._find_buffered_by_id(
            connection, event.tenant_id, event.case_id, consumer_id, event.event_id
        )
        if buffered_by_id is not None:
            sequence, stored = buffered_by_id
            if sequence != event.case_seq or not self._same_event(stored, event):
                raise ContractViolation(ErrorCode.CONFLICT, "buffered event identity conflicts")
            current, drained = self._drain(connection, position)
            if any(item.event_id == event.event_id for item in drained):
                return ProjectionIngestResult("replay", drained, current)
            return ProjectionIngestResult("buffer_gap", drained, current)

        # A sequence can exist in either ledger.  Compare the full event before
        # deciding whether the incoming row is a duplicate or a conflict.
        existing = self._event_at_sequence(
            connection,
            "aftercare_projection_applied",
            event.tenant_id,
            event.case_id,
            consumer_id,
            event.case_seq,
        )
        if existing is not None:
            event_id, stored = existing
            if event_id != event.event_id or not self._same_event(stored, event):
                raise ContractViolation(ErrorCode.CONFLICT, "applied sequence conflicts")
            return ProjectionIngestResult("replay", (), position)
        existing = self._event_at_sequence(
            connection,
            "aftercare_projection_buffer",
            event.tenant_id,
            event.case_id,
            consumer_id,
            event.case_seq,
        )
        if existing is not None:
            event_id, stored = existing
            if event_id != event.event_id or not self._same_event(stored, event):
                raise ContractViolation(ErrorCode.CONFLICT, "buffer sequence conflicts")
            current, drained = self._drain(connection, position)
            return ProjectionIngestResult("buffer_gap", drained, current)

        decision = projection_decision(position, event, applied_at_sequence=None)
        if decision == "buffer_gap":
            connection.execute(
                "INSERT INTO aftercare_projection_buffer"
                "(tenant_id,case_id,consumer_id,case_seq,event_id,payload) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    event.tenant_id,
                    event.case_id,
                    consumer_id,
                    event.case_seq,
                    event.event_id,
                    Jsonb(event.model_dump(mode="json")),
                ),
            )
            return ProjectionIngestResult("buffer_gap", (), position)

        self._record_applied(connection, event, consumer_id)
        position = position.model_copy(update={"last_case_seq": event.case_seq})
        connection.execute(
            "UPDATE aftercare_projection_positions SET last_case_seq=%s,"
            "updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND case_id=%s AND consumer_id=%s",
            (position.last_case_seq, position.tenant_id, position.case_id, consumer_id),
        )
        position, drained = self._drain(connection, position)
        return ProjectionIngestResult("apply", (event, *drained), position)

    def position(
        self,
        connection: psycopg.Connection[Any],
        *,
        tenant_id: str,
        case_id: str,
        consumer_id: str,
    ) -> ProjectionPosition | None:
        row = connection.execute(
            "SELECT tenant_id,case_id,consumer_id,last_case_seq "
            "FROM aftercare_projection_positions "
            "WHERE tenant_id=%s AND case_id=%s AND consumer_id=%s",
            (tenant_id, case_id, consumer_id),
        ).fetchone()
        if row is None:
            return None
        return ProjectionPosition.model_validate(dict(zip(_POSITION_FIELDS, row, strict=False)))

    def buffered(
        self,
        connection: psycopg.Connection[Any],
        *,
        tenant_id: str,
        case_id: str,
        consumer_id: str,
    ) -> tuple[DomainEvent, ...]:
        rows = connection.execute(
            "SELECT payload FROM aftercare_projection_buffer WHERE tenant_id=%s AND case_id=%s "
            "AND consumer_id=%s ORDER BY case_seq",
            (tenant_id, case_id, consumer_id),
        ).fetchall()
        return tuple(_decode(row[0], what="buffered") for row in rows)
