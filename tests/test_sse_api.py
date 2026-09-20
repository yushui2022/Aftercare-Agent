"""Case-scoped SSE replay endpoint checks; skipped without PostgreSQL."""

import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from aftercare_agent.api.app import create_app
from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.domain.protocol import ArtifactReference
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.persistence import Database, EventRepository, RunRepository, migrate
from aftercare_agent.runtime import PostgresEventTail


class _PollRecordingDatabase(Database):
    """A Database that records the backend each unit of work borrows."""

    def __init__(self, dsn: str) -> None:
        super().__init__(dsn)
        self.backends: list[int] = []

    @contextmanager
    def transaction(
        self, *, tenant_id: str | None = None, subject_id: str | None = None
    ) -> Iterator[psycopg.Connection[Any]]:
        with super().transaction(tenant_id=tenant_id, subject_id=subject_id) as connection:
            row = connection.execute("SELECT pg_backend_pid()").fetchone()
            assert row is not None
            self.backends.append(int(row[0]))
            yield connection


@pytest.fixture()
def client() -> Iterator[TestClient]:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    database = Database(dsn)
    tenant = "sse-api"
    with database.transaction() as connection:
        migrate(connection)
        connection.execute("DELETE FROM aftercare_outbox WHERE tenant_id=%s", (tenant,))
        connection.execute(
            "DELETE FROM aftercare_case_event_sequences WHERE tenant_id=%s", (tenant,)
        )
        connection.execute("DELETE FROM aftercare_event_payloads WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        RunRepository().create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id="case-1", order_id="order-1", version=1),
        )
        RunRepository().create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
                case_id="case-1",
                run_id="run-1",
                definition_version="v1",
                input_version=1,
            ),
        )
        event = DomainEvent(
            tenant_id=tenant,
            case_id="case-1",
            event_id="event-1",
            case_seq=1,
            event_type="case.opened",
            payload=ArtifactReference(
                tenant_id=tenant, case_id="case-1", reference_id="artifact-1", sha256="a" * 64
            ),
            correlation_id="correlation-1",
            recorded_at=datetime(2026, 9, 12, 12, tzinfo=UTC),
        )
        EventRepository().append_outbox(connection, event)
    with TestClient(create_app(database, allow_synthetic=True)) as value:
        yield value


def test_sse_replay_is_scoped_and_resumable(client: TestClient) -> None:
    headers = {"X-Synthetic-Tenant": "sse-api", "X-Synthetic-Subject": "reader"}
    first = client.get("/v1/cases/case-1/events", headers=headers)
    assert first.status_code == 200
    assert "id: 1" in first.text
    assert "event: case.opened" in first.text
    resumed = client.get("/v1/cases/case-1/events", headers={**headers, "Last-Event-ID": "1"})
    assert resumed.status_code == 200
    assert resumed.text == ""
    foreign = client.get(
        "/v1/cases/case-1/events",
        headers={"X-Synthetic-Tenant": "other-tenant", "X-Synthetic-Subject": "reader"},
    )
    assert foreign.status_code == 200
    assert foreign.text == ""


def test_sse_bounded_postgres_tail_replays_after_cursor(client: TestClient) -> None:
    headers = {"X-Synthetic-Tenant": "sse-api", "X-Synthetic-Subject": "reader"}
    # The endpoint's follow path is exercised with zero wait: it must use the
    # polling tail shape while still closing immediately when no new row exists.
    response = client.get(
        "/v1/cases/case-1/events?after=1&follow=true&wait_seconds=0",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.text == ""


def test_sse_tail_rejects_unbounded_wait(client: TestClient) -> None:
    headers = {"X-Synthetic-Tenant": "sse-api", "X-Synthetic-Subject": "reader"}
    response = client.get(
        "/v1/cases/case-1/events?follow=true&wait_seconds=61",
        headers=headers,
    )
    assert response.status_code == 400


def test_postgres_tail_observes_an_event_committed_after_it_starts(client: TestClient) -> None:
    del client  # Fixture creates the scoped Case and its seq=1 event.
    database = Database(os.environ["DATABASE_URL"])
    tail = PostgresEventTail(database, poll_seconds=0.01)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            lambda: list(
                tail.stream(
                    tenant_id="sse-api",
                    case_id="case-1",
                    after_case_seq=1,
                    wait_seconds=0.3,
                )
            )
        )
        time.sleep(0.05)
        with database.transaction() as connection:
            EventRepository().append_outbox(
                connection,
                DomainEvent(
                    tenant_id="sse-api",
                    case_id="case-1",
                    event_id="event-2",
                    case_seq=2,
                    event_type="input.received",
                    payload=ArtifactReference(
                        tenant_id="sse-api",
                        case_id="case-1",
                        reference_id="artifact-2",
                        sha256="b" * 64,
                    ),
                    correlation_id="correlation-1",
                    recorded_at=datetime(2026, 9, 12, 12, tzinfo=UTC),
                ),
            )
        events = pending.result(timeout=2)
    assert [event.case_seq for event in events] == [2]


def test_postgres_tail_polls_on_one_borrowed_connection(client: TestClient) -> None:
    """Polling must cost a round trip, not a connection.

    The tail polls inside a wall-clock window, so a poll that pays a TCP and
    authentication handshake (p50 115 ms where this was measured) leaves the
    window able to miss an event committed in the middle of it.  Polling every
    10 ms gives the pool ten milliseconds to hand the connection back, so the
    steady state is one backend; a regression to one connection per poll is
    visible here as dozens.
    """
    del client  # Fixture creates the scoped Case and its seq=1 event.
    database = _PollRecordingDatabase(os.environ["DATABASE_URL"])
    try:
        tail = PostgresEventTail(database, poll_seconds=0.01)
        events = list(
            tail.stream(
                tenant_id="sse-api",
                case_id="case-1",
                after_case_seq=1,
                wait_seconds=0.3,
            )
        )
        assert events == []
        assert len(database.backends) >= 3
        assert len(set(database.backends)) <= 2
    finally:
        database.close()
