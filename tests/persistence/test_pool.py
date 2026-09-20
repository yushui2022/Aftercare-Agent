"""Connection-pool behaviour against a real PostgreSQL; skipped without DATABASE_URL.

These checks are about physical connections, so they only mean something
against a real server: the point of the pool is that a unit of work stops
paying a TCP and authentication handshake, and only the server can say how
many backends a borrower actually reached.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg_pool import PoolTimeout

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.persistence import Database


def _backend_pid(connection: psycopg.Connection[Any]) -> int:
    row = connection.execute("SELECT pg_backend_pid()").fetchone()
    assert row is not None
    return int(row[0])


def test_pooled_units_of_work_reuse_backends(dsn: str) -> None:
    """Twenty-five transactions must not cost twenty-five connections.

    The pool is capped at one connection, so it cannot grow its way out of the
    assertion: every borrow either finds the connection waiting or is handed
    the one that is coming back.  The unpooled comparison is the next test,
    and it reaches a fresh backend every time.
    """
    database = Database(dsn, min_size=1, max_size=1)
    try:
        pids = []
        for _ in range(25):
            with database.transaction() as connection:
                pids.append(_backend_pid(connection))
        assert len(set(pids)) == 1
    finally:
        database.close()


def test_transaction_tenant_context_does_not_leak_through_pool(dsn: str) -> None:
    """Tenant and subject settings are visible only inside their transaction."""
    database = Database(dsn, min_size=1, max_size=1)
    try:
        with database.transaction(tenant_id="tenant-a", subject_id="subject-a") as connection:
            row = connection.execute(
                "SELECT current_setting('aftercare.tenant_id', true), "
                "current_setting('aftercare.subject_id', true)"
            ).fetchone()
            assert row == ("tenant-a", "subject-a")

        with database.transaction(tenant_id="tenant-b") as connection:
            row = connection.execute(
                "SELECT current_setting('aftercare.tenant_id', true), "
                "current_setting('aftercare.subject_id', true)"
            ).fetchone()
            assert row == ("tenant-b", "")

        with database.transaction() as connection:
            row = connection.execute(
                "SELECT current_setting('aftercare.tenant_id', true), "
                "current_setting('aftercare.subject_id', true)"
            ).fetchone()
            assert row == ("", "")
    finally:
        database.close()


def test_direct_database_opens_one_connection_per_unit_of_work(dsn: str) -> None:
    """The unpooled mode keeps every unit of work on its own connection."""
    database = Database.direct(dsn)
    try:
        pids = []
        for _ in range(5):
            with database.transaction() as connection:
                pids.append(_backend_pid(connection))
        assert len(set(pids)) == len(pids)
    finally:
        database.close()


def test_a_released_connection_goes_back_to_the_pool(dsn: str) -> None:
    """A holder that closes its borrow returns it instead of destroying it.

    The lease heartbeat holds one connection for a slice and gives it back with
    ``release()``; if that really closed the connection, every slice would pay
    the handshake the heartbeat exists to avoid.  The pool is capped at one, so
    the next borrow has to be served by the connection that came back: a
    destroyed one would be replaced by a fresh backend with a new pid.
    """
    database = Database(dsn, min_size=1, max_size=1)
    try:
        held = database.open()
        with held.transaction():
            pid = _backend_pid(held)
        database.release(held)
        assert not held.closed
        with database.transaction() as reused:
            assert _backend_pid(reused) == pid
    finally:
        database.close()


def test_concurrent_borrows_stay_within_max_size(dsn: str) -> None:
    """Waiting work must not hold a connection: the pool caps the backends."""
    database = Database(dsn, min_size=1, max_size=2)
    barrier = Barrier(8)

    def borrow(index: int) -> int:
        del index
        barrier.wait()
        with database.transaction() as connection:
            return _backend_pid(connection)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            pids = list(pool.map(borrow, range(8)))
        assert len(set(pids)) <= database.max_size
    finally:
        database.close()


def test_an_exhausted_pool_fails_closed_and_keeps_serving(dsn: str) -> None:
    """A caller that cannot get a connection in budget is told to retry."""
    database = Database(dsn, min_size=1, max_size=1, acquire_timeout=0.5)
    try:
        held = database.open()
        pid = _backend_pid(held)
        with pytest.raises(ContractViolation) as error:
            with database.transaction():
                pass
        assert error.value.code is ErrorCode.RETRYABLE
        # The refused borrow must not have consumed the slot it never got.
        database.release(held)
        with database.transaction() as connection:
            assert _backend_pid(connection) == pid
    finally:
        database.close()


def test_a_closed_pool_refuses_further_work_and_closes_twice(dsn: str) -> None:
    database = Database(dsn, min_size=1, max_size=2)
    with database.transaction() as connection:
        _backend_pid(connection)
    database.close()
    database.close()
    with pytest.raises(RuntimeError, match="closed"):
        with database.transaction():
            pass


def test_startup_prewarms_or_fails_closed(dsn: str) -> None:
    """Startup waits for the pool, and a failed start leaves no poisoned pool."""
    reachable = Database(dsn, min_size=2, max_size=4)
    try:
        reachable.startup()
        with reachable.transaction() as connection:
            _backend_pid(connection)
    finally:
        reachable.close()

    # Same credentials, no server: the start has to fail rather than serve a
    # request that will only learn about the database when it borrows.
    # The server rejects this password, so the connection never becomes ready.
    # (A port with nothing listening is the more obvious stand-in, but on this
    # platform a dropped SYN stalls a connect for seconds; a refused login is
    # the same "could not open a connection" path without the platform delay.)
    refused = make_conninfo(dsn, password="not-the-password")
    unreachable = Database(refused, min_size=1, acquire_timeout=0.5)
    try:
        with pytest.raises(PoolTimeout):
            unreachable.startup()
        # A failed start must not leave a closed pool behind a retry.
        with pytest.raises(PoolTimeout):
            unreachable.startup()
    finally:
        unreachable.close()
