"""Short PostgreSQL transactions over a bounded connection pool."""

import hashlib
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from aftercare_agent.domain.common import ContractViolation, ErrorCode

_MIGRATIONS = Path(__file__).with_name("migrations")

# Sized for the deployment's concurrency, not for its queue: a unit of work
# holds a slot for the length of one short transaction, so waiting work costs
# no connection at all.  Measured input (development host): opening a
# connection costs p50 115 ms while the renewal transaction it used to wrap
# costs p50 0.8 ms.
DEFAULT_MIN_SIZE = 1
DEFAULT_MAX_SIZE = 8
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class PoolStats:
    """One snapshot of a process's pool.

    Counters are cumulative since the pool opened and start over with the
    process; a caller that wants a rate differences two snapshots.  Nothing
    here describes the work being done: no tenant, case, statement or DSN is
    reachable from a snapshot, so it is safe to publish as a metric.
    """

    min_size: int
    max_size: int
    size: int
    available: int
    waiting: int
    requests: int
    queued: int
    request_errors: int
    wait_ms: int
    connections: int
    connection_errors: int
    connections_lost: int

    @property
    def in_use(self) -> int:
        """Slots that are out with a borrower right now."""
        return self.size - self.available


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


class Database:
    """A bounded connection pool; callers keep transactions short."""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int | None = None,
        max_size: int | None = None,
        acquire_timeout: float | None = None,
    ) -> None:
        """Configure a pool without opening it.

        Sizes come from the caller, or from ``AFTERCARE_DB_POOL_MIN_SIZE``,
        ``AFTERCARE_DB_POOL_MAX_SIZE`` and
        ``AFTERCARE_DB_ACQUIRE_TIMEOUT_SECONDS`` when the caller leaves them
        unset.  Nothing connects here: :meth:`startup` opens the pool, and a
        borrow opens it on demand for callers that never call ``startup()``.
        """
        if not dsn:
            raise ValueError("DATABASE_URL is required")
        self.dsn = dsn
        self.min_size = (
            _int_from_env("AFTERCARE_DB_POOL_MIN_SIZE", DEFAULT_MIN_SIZE)
            if min_size is None
            else min_size
        )
        self.max_size = (
            _int_from_env("AFTERCARE_DB_POOL_MAX_SIZE", DEFAULT_MAX_SIZE)
            if max_size is None
            else max_size
        )
        self.acquire_timeout = (
            _float_from_env("AFTERCARE_DB_ACQUIRE_TIMEOUT_SECONDS", DEFAULT_ACQUIRE_TIMEOUT_SECONDS)
            if acquire_timeout is None
            else acquire_timeout
        )
        if self.min_size < 0 or self.max_size < max(self.min_size, 1):
            raise ValueError("pool size must satisfy 0 <= min_size <= max_size and max_size >= 1")
        if not self.acquire_timeout > 0:
            raise ValueError("acquire_timeout must be positive")
        self._pooled = True
        self._closed = False
        self._pool: ConnectionPool[psycopg.Connection[Any]] | None = None
        self._pool_lock = threading.Lock()

    @classmethod
    def direct(cls, dsn: str) -> "Database":
        """A Database without a pool: one connection per unit of work.

        Tests and one-off scripts use this to keep a short-lived process from
        starting pool threads, and to hold a single unit of work to its own
        physical connection.
        """
        database = cls(dsn)
        database._pooled = False
        return database

    def startup(self, *, timeout: float | None = None) -> None:
        """Open the pool and wait for its first connections.

        Pre-warming keeps the first unit of work from paying a connect, and a
        database that is unreachable fails the process start instead of the
        first request.

        *timeout* defaults to the borrow budget, so a start that cannot warm
        the pool fails on the same footing as a unit of work that cannot
        borrow one.  A caller that deliberately runs a very short borrow
        budget -- the capacity probe measuring saturation -- passes a longer
        start timeout instead of making every borrower wait that long.
        """
        pool = self._ensure_pool()
        if pool is None:
            return
        budget = self.acquire_timeout if timeout is None else timeout
        try:
            pool.open(wait=True, timeout=budget)
        except PoolTimeout:
            # ``wait()`` closes the pool it could not fill; forget it so a
            # later attempt builds a fresh one instead of meeting a closed one.
            with self._pool_lock:
                self._pool = None
            raise

    def close(self, *, timeout: float = 5.0) -> None:
        """Close the pool; idempotent, and safe after a failed startup.

        A connection a caller still holds is closed when it is released, so a
        holder does not have to be torn down before the pool is.
        """
        with self._pool_lock:
            pool, self._pool = self._pool, None
            self._closed = True
        if pool is not None:
            pool.close(timeout=timeout)

    def stats(self) -> PoolStats | None:
        """Snapshot the pool, or ``None`` when this Database has no pool.

        The snapshot is a diagnostic read, not a health check: a caller that
        cannot borrow still gets a truthful picture of how many slots were in
        use and how many borrowers were waiting when it gave up.
        """
        with self._pool_lock:
            pool = self._pool
        if pool is None:
            return None
        raw = pool.get_stats()
        return PoolStats(
            min_size=int(raw.get("pool_min", self.min_size)),
            max_size=int(raw.get("pool_max", self.max_size)),
            size=int(raw.get("pool_size", 0)),
            available=int(raw.get("pool_available", 0)),
            waiting=int(raw.get("requests_waiting", 0)),
            requests=int(raw.get("requests_num", 0)),
            queued=int(raw.get("requests_queued", 0)),
            request_errors=int(raw.get("requests_errors", 0)),
            wait_ms=int(raw.get("requests_wait_ms", 0)),
            connections=int(raw.get("connections_num", 0)),
            connection_errors=int(raw.get("connections_errors", 0)),
            connections_lost=int(raw.get("connections_lost", 0)),
        )

    def _ensure_pool(self) -> ConnectionPool[psycopg.Connection[Any]] | None:
        with self._pool_lock:
            if self._closed:
                raise RuntimeError("the database pool is closed")
            if not self._pooled:
                return None
            if self._pool is None:
                self._pool = ConnectionPool(
                    self.dsn,
                    min_size=self.min_size,
                    max_size=self.max_size,
                    # A waiter that is not served inside its budget fails closed
                    # instead of queueing behind an unbounded wait.
                    timeout=self.acquire_timeout,
                    # Reused connections are checked before they are handed out,
                    # so a database restart costs one discarded connection and
                    # not a failed unit of work.
                    check=ConnectionPool.check_connection,
                    # A borrowed connection that a caller closes by reflex goes
                    # back to the pool instead of being destroyed.
                    close_returns=True,
                    open=False,
                )
                # ``open=False`` above keeps construction side-effect free; the
                # first borrow (or startup()) opens the pool explicitly instead
                # of leaving psycopg_pool to open it with a DeprecationWarning.
                self._pool.open()
            return self._pool

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """Borrow one connection, commit or roll back, then give it back."""
        connection = self.open()
        try:
            # ``with`` stays the commit/rollback boundary callers already had;
            # it does not close a connection that belongs to a pool.
            with connection:
                yield connection
        finally:
            self.release(connection)

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[Any]]:
        with self.connection() as connection:
            with connection.transaction():
                yield connection

    def open(self) -> psycopg.Connection[Any]:
        """Take a connection the caller owns until it calls :meth:`release`.

        ``connection()`` borrows one connection per unit of work and gives it
        back.  A holder that has to outlive many short transactions -- the
        Worker lease heartbeat is the only one today -- instead takes the
        connection here and is responsible for releasing it.  Both paths go
        through this method so the pool has a single seam to change.

        A pooled connection belongs to one thread for the length of the borrow.
        The next borrow may hand it to another thread, and the pool rolls back
        a connection returned mid-transaction but does not reset session state,
        so no borrower may leave ``SET``, ``LISTEN`` or ``search_path`` behind
        for the next one (``SET LOCAL`` reverts with its transaction and is
        fine).  A long-lived holder occupies one slot for as long as it holds,
        which is why ``max_size`` defaults above ``min_size``.
        """
        pool = self._ensure_pool()
        if pool is None:
            return psycopg.connect(self.dsn)
        try:
            return pool.getconn()
        except PoolClosed as exc:
            raise ContractViolation(ErrorCode.RETRYABLE, "database pool is closed") from exc
        except PoolTimeout as exc:
            raise ContractViolation(
                ErrorCode.RETRYABLE,
                f"no database connection within {self.acquire_timeout:g}s",
            ) from exc

    def release(self, connection: psycopg.Connection[Any]) -> None:
        """Give back a connection taken from :meth:`open`.

        The pool is built with ``close_returns=True``, so ``close()`` on a
        borrowed connection returns it to the pool, and closes for real a
        connection that came straight from ``psycopg.connect()``.  Releasing
        after the pool was closed is a close too, so a shutdown cannot lose a
        connection that was still out with a holder.
        """
        connection.close()


def migrate(connection: psycopg.Connection[Any]) -> None:
    # Transaction-scoped advisory lock serializes API/Worker startup migrations
    # without holding an application table lock after this call returns.
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (734_291_117,))
    connection.execute(
        "CREATE TABLE IF NOT EXISTS aftercare_schema_migrations ("
        "version integer PRIMARY KEY, checksum char(64), "
        "applied_at timestamptz NOT NULL DEFAULT clock_timestamp())"
    )
    connection.execute(
        "ALTER TABLE aftercare_schema_migrations ADD COLUMN IF NOT EXISTS checksum char(64)"
    )
    paths = sorted(_MIGRATIONS.glob("*.sql"))
    checksums = {
        int(path.name.split("_", 1)[0]): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    known = {int(path.name.split("_", 1)[0]) for path in paths}
    applied = {
        int(row[0])
        for row in connection.execute("SELECT version FROM aftercare_schema_migrations").fetchall()
    }
    if unknown := applied - known:
        raise RuntimeError(f"unknown migration versions: {sorted(unknown)}")
    for version, checksum in connection.execute(
        "SELECT version,checksum FROM aftercare_schema_migrations"
    ).fetchall():
        # Rows from the pre-checksum schema are safely pinned on first startup;
        # once pinned, any historical SQL drift is a hard deployment error.
        if checksum is None:
            connection.execute(
                "UPDATE aftercare_schema_migrations SET checksum=%s WHERE version=%s",
                (checksums[int(version)], version),
            )
        elif checksum != checksums[int(version)]:
            raise RuntimeError(f"migration checksum changed: {version}")
    for path in paths:
        version = int(path.name.split("_", 1)[0])
        if version not in applied:
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO aftercare_schema_migrations(version,checksum) VALUES (%s,%s)",
                (version, checksums[version]),
            )
