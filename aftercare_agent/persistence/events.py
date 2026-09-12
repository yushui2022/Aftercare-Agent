"""Transactional Inbox/Outbox primitives with explicit replay semantics."""

import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.domain.waits import InboxSignal, check_inbox_replay


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
