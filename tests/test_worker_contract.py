"""Database-independent Worker lease safety checks."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from typing import Any, cast

import pytest

import aftercare_agent.runtime.worker as worker_module
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import ExecutionClaim
from aftercare_agent.persistence import Database, SlotReservation
from aftercare_agent.runtime import LeaseHeartbeat


class _Connection:
    """Stand-in for a borrowed psycopg connection."""

    def __init__(self) -> None:
        self.transactions = 0

    @contextmanager
    def transaction(self) -> Iterator["_Connection"]:
        self.transactions += 1
        yield self


class _Database:
    """Record every connection a holder takes for itself and gives back.

    Only ``open()`` and ``release()`` exist: a holder renewing a lease inside a
    deadline has to keep its connection, so quietly borrowing a fresh one per
    tick is the regression this fake is here to catch, and keeping a borrowed
    connection for itself instead of releasing it is the other.
    """

    def __init__(self) -> None:
        self.connections: list[_Connection] = []
        self.released: list[_Connection] = []

    def open(self) -> _Connection:
        connection = _Connection()
        self.connections.append(connection)
        return connection

    def release(self, connection: _Connection) -> None:
        self.released.append(connection)


class _RunRepository:
    def renew(self, connection: Any, claim: ExecutionClaim, lease: timedelta) -> None:
        return None


class _AdmissionRepository:
    def __init__(self, called: Event) -> None:
        self._called = called

    def renew_slot(self, connection: Any, claim: ExecutionClaim, lease: timedelta) -> datetime:
        self._called.set()
        raise ContractViolation(ErrorCode.LEASE_LOST, "execution slot lease lost")


def test_heartbeat_stops_when_reserved_slot_renewal_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing/expired slot cannot be treated as a successful heartbeat."""
    called = Event()
    monkeypatch.setattr(worker_module, "RunRepository", _RunRepository)
    monkeypatch.setattr(
        worker_module,
        "AdmissionRepository",
        lambda: _AdmissionRepository(called),
    )
    claim = ExecutionClaim(
        tenant_id="tenant-1",
        case_id="case-1",
        run_id="run-1",
        owner="worker-1",
        fencing_token=1,
    )
    slot = SlotReservation(
        tenant_id=claim.tenant_id,
        case_id=claim.case_id,
        run_id=claim.run_id,
        owner=claim.owner,
        fencing_token=claim.fencing_token,
        lease_until=datetime.now(UTC) + timedelta(seconds=1),
    )
    database = _Database()
    heartbeat = LeaseHeartbeat(
        cast(Database, database),
        claim,
        timedelta(seconds=1),
        slot=slot,
        interval=timedelta(milliseconds=1),
    )

    heartbeat._run()

    assert called.is_set()
    failure = heartbeat.failure
    assert isinstance(failure, ContractViolation)
    assert failure.code is ErrorCode.LEASE_LOST
    assert database.released == list(database.connections)


def _claim() -> ExecutionClaim:
    return ExecutionClaim(
        tenant_id="tenant-1",
        case_id="case-1",
        run_id="run-1",
        owner="worker-1",
        fencing_token=1,
    )


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.005)
    raise AssertionError("the heartbeat did not reach the expected state in time")


def test_heartbeat_renews_before_the_first_interval_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window the claim already holds has to cover opening the connection.

    Sleeping a full interval first would hand a third of that window to a
    timer and leave the handshake competing with the deadline it must beat.
    """
    monkeypatch.setattr(worker_module, "RunRepository", _RunRepository)
    database = _Database()
    heartbeat = LeaseHeartbeat(
        cast(Database, database),
        _claim(),
        timedelta(seconds=60),
        interval=timedelta(seconds=30),
    )

    heartbeat.start()
    try:
        _wait_until(lambda: bool(database.connections))
    finally:
        heartbeat.stop()

    assert heartbeat.failure is None
    assert [connection.transactions for connection in database.connections] == [1]
    assert database.released == list(database.connections)


def test_heartbeat_reuses_one_connection_across_renewals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renewal must not pay a handshake inside the deadline it serves."""
    monkeypatch.setattr(worker_module, "RunRepository", _RunRepository)
    database = _Database()
    heartbeat = LeaseHeartbeat(
        cast(Database, database),
        _claim(),
        timedelta(seconds=1),
        interval=timedelta(milliseconds=5),
    )

    heartbeat.start()
    try:
        _wait_until(
            lambda: bool(database.connections) and database.connections[0].transactions >= 3
        )
    finally:
        heartbeat.stop()

    assert heartbeat.failure is None
    assert len(database.connections) == 1
    assert database.released == list(database.connections)
