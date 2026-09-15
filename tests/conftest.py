"""PostgreSQL fixtures shared by the suites that need a real server.

One definition per fixture.  These three used to be copy-pasted into every
file that touched a database -- eighteen identical ``db`` fixtures, three
``database``, two ``dsn`` -- which is how a test suite grows without covering
anything new.  A file that needs different setup (seeded rows, extra cleanup,
a strict instead of skipping check) still defines its own fixture locally and
shadows the one here.
"""

import os

import pytest

from aftercare_agent.persistence import Database, migrate


@pytest.fixture()
def dsn() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        pytest.skip("DATABASE_URL is not configured")
    return value


@pytest.fixture()
def database(dsn: str) -> Database:
    """A connection to the configured server, with migrations applied."""
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


@pytest.fixture()
def db(database: Database) -> Database:
    """The same migrated connection; the persistence suites call it ``db``."""
    return database
