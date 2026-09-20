"""The API owns startup validation and cleanup through one lifespan."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import aftercare_agent.api.app as api_module
from aftercare_agent.api.app import create_app
from aftercare_agent.persistence import Database


def test_lifespan_validates_before_serving_and_cleans_up_after_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    database = Database("postgresql://unused")

    monkeypatch.setattr(Database, "startup", lambda _self: events.append("startup"))

    @contextmanager
    def fake_transaction(_self: Database) -> Iterator[object]:
        events.append("transaction.open")
        yield object()
        events.append("transaction.close")

    monkeypatch.setattr(Database, "transaction", fake_transaction)
    monkeypatch.setattr(api_module, "migrate", lambda _connection: events.append("migrate"))
    monkeypatch.setattr(
        api_module,
        "assert_schema_current",
        lambda _connection: events.append("schema"),
    )

    app = create_app(
        database,
        auto_migrate=True,
        on_shutdown=lambda: events.append("shutdown"),
    )
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert events == ["startup", "transaction.open", "migrate", "schema", "transaction.close"]

    assert events == [
        "startup",
        "transaction.open",
        "migrate",
        "schema",
        "transaction.close",
        "shutdown",
    ]
