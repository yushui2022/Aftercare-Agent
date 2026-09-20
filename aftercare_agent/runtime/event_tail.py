"""Broker-neutral, bounded event tailing backed by PostgreSQL polling.

This is a reference tail for development and small deployments.  It never
holds a database transaction while sleeping and it uses the same case cursor
as SSE replay.  A Kafka/NATS/Redis adapter can implement the same shape later
without changing the API's replay contract.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from math import isfinite
from time import monotonic, sleep

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.persistence import Database, EventRepository


@dataclass(frozen=True)
class PostgresEventTail:
    """Poll a case's committed Outbox rows for a bounded period."""

    database: Database
    poll_seconds: float = 0.5

    @staticmethod
    def validate(
        *, after_case_seq: int, limit: int, wait_seconds: float, poll_seconds: float
    ) -> None:
        """Validate options before a streaming response starts."""
        if type(after_case_seq) is not int or after_case_seq < 0:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "after_case_seq must be non-negative")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "event limit must be between 1 and 500"
            )
        if not isfinite(wait_seconds) or wait_seconds < 0 or wait_seconds > 60:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "wait_seconds must be between 0 and 60"
            )
        if not isfinite(poll_seconds) or poll_seconds <= 0 or poll_seconds > 10:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, "poll_seconds must be between 0 and 10"
            )

    def stream(
        self,
        *,
        tenant_id: str,
        case_id: str,
        after_case_seq: int = 0,
        limit: int = 100,
        wait_seconds: float = 15.0,
    ) -> Iterator[DomainEvent]:
        self.validate(
            after_case_seq=after_case_seq,
            limit=limit,
            wait_seconds=wait_seconds,
            poll_seconds=self.poll_seconds,
        )

        cursor = after_case_seq
        deadline = monotonic() + wait_seconds
        while True:
            with self.database.transaction(tenant_id=tenant_id) as connection:
                events = EventRepository().list_case_events(
                    connection,
                    tenant_id=tenant_id,
                    case_id=case_id,
                    after_case_seq=cursor,
                    limit=limit,
                )
            if events:
                for event in events:
                    cursor = event.case_seq
                    yield event
                if monotonic() >= deadline:
                    return
                # Drain all currently committed rows before sleeping.  This
                # also prevents a busy case from being delayed by poll_seconds.
                continue
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            sleep(min(self.poll_seconds, remaining))

    async def stream_async(
        self,
        *,
        tenant_id: str,
        case_id: str,
        after_case_seq: int = 0,
        limit: int = 100,
        wait_seconds: float = 15.0,
    ) -> AsyncIterator[DomainEvent]:
        """Asynchronously tail committed rows without blocking the event loop.

        ``Database`` intentionally exposes a synchronous psycopg API.  Each
        short poll is therefore delegated to a worker thread, while the
        potentially long wait happens with ``asyncio.sleep``.  This keeps one
        FastAPI event-loop thread available for many idle SSE clients instead
        of pinning a thread per connection during the polling interval.
        """
        self.validate(
            after_case_seq=after_case_seq,
            limit=limit,
            wait_seconds=wait_seconds,
            poll_seconds=self.poll_seconds,
        )

        cursor = after_case_seq
        deadline = monotonic() + wait_seconds
        while True:
            events = await asyncio.to_thread(self._poll_once, tenant_id, case_id, cursor, limit)
            if events:
                for event in events:
                    cursor = event.case_seq
                    yield event
                if monotonic() >= deadline:
                    return
                continue
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self.poll_seconds, remaining))

    def _poll_once(
        self, tenant_id: str, case_id: str, cursor: int, limit: int
    ) -> tuple[DomainEvent, ...]:
        """Read one bounded batch in a short transaction (thread target)."""
        with self.database.transaction(tenant_id=tenant_id) as connection:
            return EventRepository().list_case_events(
                connection,
                tenant_id=tenant_id,
                case_id=case_id,
                after_case_seq=cursor,
                limit=limit,
            )
