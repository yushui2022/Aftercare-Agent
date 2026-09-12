"""Short PostgreSQL transactions and explicit migrations."""

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
        with psycopg.connect(self.dsn) as connection:
            yield connection

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection[Any]]:
        with self.connection() as connection:
            with connection.transaction():
                yield connection


def migrate(connection: psycopg.Connection[Any]) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS aftercare_schema_migrations ("
        "version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT clock_timestamp())"
    )
    paths = sorted(_MIGRATIONS.glob("*.sql"))
    known = {int(path.name.split("_", 1)[0]) for path in paths}
    applied = {
        int(row[0])
        for row in connection.execute("SELECT version FROM aftercare_schema_migrations").fetchall()
    }
    if unknown := applied - known:
        raise RuntimeError(f"unknown migration versions: {sorted(unknown)}")
    for path in paths:
        version = int(path.name.split("_", 1)[0])
        if version not in applied:
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO aftercare_schema_migrations(version) VALUES (%s)", (version,)
            )
