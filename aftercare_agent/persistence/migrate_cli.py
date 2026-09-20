"""Explicit database migration job for Aftercare and its embedded EGM schema."""

import argparse
import json
from collections.abc import Sequence
from typing import Any

import psycopg

from aftercare_agent.config import environment_secret

from .db import Database, assert_schema_current, latest_schema_version, migrate

EGM_SCHEMA_VERSION = 1


def assert_egm_schema_current(connection: psycopg.Connection[Any]) -> None:
    """Verify the embedded EGM schema without creating or changing objects."""
    relation = connection.execute("SELECT to_regclass('egm_schema_version')").fetchone()
    if relation is None or relation[0] is None:
        raise RuntimeError("EGM database schema is not installed")
    row = connection.execute("SELECT version FROM egm_schema_version WHERE singleton").fetchone()
    if row is None or int(row[0]) != EGM_SCHEMA_VERSION:
        raise RuntimeError("unsupported PostgreSQL EGM schema version")


def apply_schema(connection: psycopg.Connection[Any]) -> None:
    """Apply both schemas inside the caller's transaction, then verify them."""
    from evidence_gated_memory.storage.postgres import migrate as migrate_egm

    migrate(connection)
    migrate_egm(connection)  # type: ignore[no-untyped-call]
    assert_schema_current(connection)
    assert_egm_schema_current(connection)


def _summary() -> dict[str, int]:
    return {
        "aftercare_schema_version": latest_schema_version(),
        "egm_schema_version": EGM_SCHEMA_VERSION,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify both schemas without applying migrations",
    )
    args = parser.parse_args(argv)
    dsn = environment_secret("DATABASE_URL") or ""
    if not dsn:
        parser.error("DATABASE_URL is required")

    database = Database.direct(dsn)
    try:
        with database.transaction() as connection:
            if args.check:
                assert_schema_current(connection)
                assert_egm_schema_current(connection)
            else:
                apply_schema(connection)
    finally:
        database.close()
    print(json.dumps({"status": "current", **_summary()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
