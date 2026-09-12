"""Broker-neutral, bounded event tailing backed by PostgreSQL polling.

This is a reference tail for development and small deployments.  It never
holds a database transaction while sleeping and it uses the same case cursor
as SSE replay.  A Kafka/NATS/Redis adapter can implement the same shape later
without changing the API's replay contract.
"""

from collections.abc import Iterator
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
            with self.database.transaction() as connection:
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
