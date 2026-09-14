"""Async PostgreSQL tail semantics without requiring a live database."""

import asyncio
from datetime import UTC, datetime

import pytest

from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.domain.protocol import ArtifactReference
from aftercare_agent.runtime import PostgresEventTail


def _event(sequence: int) -> DomainEvent:
    return DomainEvent(
        tenant_id="tenant-1",
        case_id="case-1",
        event_id=f"event-{sequence}",
        case_seq=sequence,
        event_type="input.received",
        payload=ArtifactReference(
            tenant_id="tenant-1",
            case_id="case-1",
            reference_id=f"artifact-{sequence}",
            sha256="a" * 64,
        ),
        correlation_id="correlation-1",
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class _UnusedDatabase:
    """The poll method is patched, so no connection should be opened."""


@pytest.mark.asyncio
async def test_stream_async_bounded_wait_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def poll(self: PostgresEventTail, *args: object) -> tuple[DomainEvent, ...]:
        del self, args
        return ()

    monkeypatch.setattr(PostgresEventTail, "_poll_once", poll)
    tail = PostgresEventTail(_UnusedDatabase(), poll_seconds=0.01)  # type: ignore[arg-type]
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.002)

    ticker_task = asyncio.create_task(ticker())
    try:
        events = [
            event
            async for event in tail.stream_async(
                tenant_id="tenant-1", case_id="case-1", wait_seconds=0.04
            )
        ]
    finally:
        ticker_task.cancel()
        await asyncio.gather(ticker_task, return_exceptions=True)
    assert events == []
    assert ticks >= 3


@pytest.mark.asyncio
async def test_stream_async_observes_event_added_after_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending: list[DomainEvent] = []

    def poll(self: PostgresEventTail, *args: object) -> tuple[DomainEvent, ...]:
        del self, args
        events = tuple(pending)
        pending.clear()
        return events

    monkeypatch.setattr(PostgresEventTail, "_poll_once", poll)
    tail = PostgresEventTail(_UnusedDatabase(), poll_seconds=0.005)  # type: ignore[arg-type]

    async def publish() -> None:
        await asyncio.sleep(0.015)
        pending.append(_event(2))

    publisher = asyncio.create_task(publish())
    try:
        events = [
            event
            async for event in tail.stream_async(
                tenant_id="tenant-1", case_id="case-1", after_case_seq=1, wait_seconds=0.1
            )
        ]
    finally:
        await publisher
    assert [event.case_seq for event in events] == [2]
