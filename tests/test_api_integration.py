"""A1-02 HTTP integration checks; skipped without an isolated DATABASE_URL."""

import os
from collections.abc import Iterator
from typing import cast

import pytest
from fastapi.testclient import TestClient

from aftercare_agent.api.app import create_app
from aftercare_agent.auth import AuthContext, JwtJwksVerifier
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.persistence import Database


@pytest.fixture()
def database() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    return Database(dsn)


@pytest.fixture()
def client(database: Database) -> Iterator[TestClient]:
    app = create_app(database, allow_synthetic=True)
    with TestClient(app) as value:
        yield value


def test_open_replay_and_cross_tenant_read_are_scoped(client: TestClient) -> None:
    headers = {
        "X-Synthetic-Tenant": "api-tenant",
        "X-Synthetic-Subject": "api-subject",
        "Idempotency-Key": "api-idem",
    }
    body = {
        "order_id": "api-order",
        "channel": "synthetic",
        "message_ref": "api-message",
        "message_sha256": "a" * 64,
    }
    first = client.post("/v1/cases", headers=headers, json=body)
    assert first.status_code == 201
    replay = client.post("/v1/cases", headers=headers, json=body)
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    changed = client.post(
        "/v1/cases",
        headers=headers,
        json={**body, "message_sha256": "b" * 64},
    )
    assert changed.status_code == 409
    identifiers = first.json()
    foreign = client.get(
        f"/v1/cases/{identifiers['case_id']}/runs/{identifiers['run_id']}",
        headers={"X-Synthetic-Tenant": "other-tenant", "X-Synthetic-Subject": "other-subject"},
    )
    assert foreign.status_code == 403


def test_synthetic_identity_is_not_enabled_in_production_mode() -> None:
    # A separately-created app with the same database must reject synthetic auth by default.
    # The fixture app remains enabled solely for deterministic local integration tests.
    from aftercare_agent.api.app import create_app

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    app = create_app(Database(dsn))
    with TestClient(app) as production_client:
        response = production_client.get(
            "/v1/cases/case/runs/run",
            headers={"X-Synthetic-Tenant": "api-tenant", "X-Synthetic-Subject": "api-subject"},
        )
    assert response.status_code == 401


class _StubBearerVerifier:
    """Small seam test double; JWT/JWKS behavior is covered separately."""

    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject

    def verify(self, authorization: str) -> AuthContext:
        if self.reject or authorization != "Bearer test-token":
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid bearer token")
        return AuthContext(
            subject_id="oidc-subject",
            tenant_id="api-tenant",
            permissions=frozenset({"case:read"}),
            case_ids=frozenset({"case"}),
        )


def test_bearer_path_precedes_synthetic_headers(database: Database, client: TestClient) -> None:
    """A rejected bearer must not become an accepted synthetic request."""
    from aftercare_agent.api.app import create_app

    app = create_app(
        database,
        allow_synthetic=True,
        oidc_verifier=cast(JwtJwksVerifier, _StubBearerVerifier()),
    )
    with TestClient(app) as bearer_client:
        rejected = bearer_client.get(
            "/v1/cases/case/runs/run",
            headers={
                "Authorization": "Bearer wrong-token",
                "X-Synthetic-Tenant": "api-tenant",
                "X-Synthetic-Subject": "synthetic-subject",
            },
        )
        synthetic_only = bearer_client.get(
            "/v1/cases/case/runs/run",
            headers={
                "X-Synthetic-Tenant": "api-tenant",
                "X-Synthetic-Subject": "synthetic-subject",
            },
        )
    assert rejected.status_code == 401
    assert synthetic_only.status_code == 401
