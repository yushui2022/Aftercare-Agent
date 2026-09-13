"""A1-02 HTTP integration checks; skipped without an isolated DATABASE_URL."""

import os
from collections.abc import Iterator
from typing import cast

import pytest
from fastapi.testclient import TestClient

from aftercare_agent.api.app import create_app
from aftercare_agent.auth import AuthContext, JwtJwksVerifier
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.persistence import CaseGrantRepository, Database, RunRepository, migrate


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

    def __init__(self, *, reject: bool = False, tenant_id: str = "api-tenant") -> None:
        self.reject = reject
        self.tenant_id = tenant_id

    def verify(self, authorization: str) -> AuthContext:
        if self.reject or authorization != "Bearer test-token":
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid bearer token")
        return AuthContext(
            subject_id="oidc-subject",
            tenant_id=self.tenant_id,
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


def test_bearer_case_access_requires_database_grant_and_honors_revoke(
    database: Database,
) -> None:
    tenant = "api-grant-tenant"
    with database.transaction() as connection:
        migrate(connection)
        connection.execute("DELETE FROM aftercare_case_grants WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        RunRepository().create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id="case", order_id="order", version=1),
        )
        RunRepository().create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
                case_id="case",
                run_id="run",
                definition_version="v1",
                input_version=1,
            ),
        )
        grant = CaseGrantRepository().grant(
            connection,
            tenant_id=tenant,
            subject_id="oidc-subject",
            case_id="case",
            permissions=("case:read",),
            granted_by="auth-admin",
        )
    from aftercare_agent.api.app import create_app

    app = create_app(
        database,
        oidc_verifier=cast(JwtJwksVerifier, _StubBearerVerifier(tenant_id="api-grant-tenant")),
    )
    with TestClient(app) as bearer_client:
        allowed = bearer_client.get(
            "/v1/cases/case/runs/run", headers={"Authorization": "Bearer test-token"}
        )
    assert allowed.status_code == 200
    with database.transaction() as connection:
        CaseGrantRepository().revoke(
            connection,
            tenant_id=tenant,
            subject_id="oidc-subject",
            case_id="case",
            revoked_by="auth-admin",
            expected_revision=grant.revision,
        )
    with TestClient(app) as bearer_client:
        denied = bearer_client.get(
            "/v1/cases/case/runs/run", headers={"Authorization": "Bearer test-token"}
        )
    assert denied.status_code == 403
