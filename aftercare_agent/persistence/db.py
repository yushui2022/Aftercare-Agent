"""Short PostgreSQL transactions and explicit migrations."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg

_MIGRATIONS = Path(__file__).with_name("migrations")


class Database:
    """Connection factory; callers keep transactions short."""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("DATABASE_URL is required")
        self.dsn = dsn

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        with self.open() as connection:
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[Any]]:
        with self.connection() as connection:
            with connection.transaction():
                yield connection

    def open(self) -> psycopg.Connection[Any]:
        """Open a connection the caller owns and closes.

        ``connection()`` borrows one connection per unit of work and gives it
        back.  A holder that has to outlive many short transactions -- the
        Worker lease heartbeat is the only one today -- instead takes
        ownership here and is responsible for closing it.  Both paths go
        through this method so a future pool has a single seam to change.
        """
        return psycopg.connect(self.dsn)


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
