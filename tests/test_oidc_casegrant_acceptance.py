"""Acceptance of the real bearer and database CaseGrant boundary.

This intentionally uses the production JWT/JWKS verifier with a deterministic
JWKS transport.  It does not contact an external IdP, but it exercises the
same signature, claims, API and PostgreSQL grant path used in deployment.
"""

import base64
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from aftercare_agent.api.app import create_app
from aftercare_agent.auth.oidc import JwtJwksVerifier, JwtVerifierConfig
from aftercare_agent.domain.protocol import Checkpoint, RemainingBudget
from aftercare_agent.domain.reviews import ReviewRequest
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.persistence import (
    CaseGrantRepository,
    Database,
    ReviewRepository,
    RunRepository,
)

ISSUER = "https://idp.example"
AUDIENCE = "aftercare-api"


def _base64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _token(
    private: rsa.RSAPrivateKey,
    *,
    scope: str = "case:read",
    tenant_id: str = "oidc-acceptance-tenant",
    case_id: str = "oidc-case-1",
) -> str:
    issued_at = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": ISSUER,
            "sub": "oidc-user-1",
            "aud": AUDIENCE,
            "tenant_id": tenant_id,
            "scope": scope,
            "case_ids": [case_id],
            "iat": int(issued_at.timestamp()),
            "exp": int((issued_at + timedelta(minutes=5)).timestamp()),
        },
        private,
        algorithm="RS256",
        headers={"kid": "acceptance-key", "typ": "JWT"},
    )


def _verifier(private: rsa.RSAPrivateKey) -> JwtJwksVerifier:
    numbers = private.public_key().public_numbers()
    jwk: dict[str, object] = {
        "kty": "RSA",
        "kid": "acceptance-key",
        "alg": "RS256",
        "use": "sig",
        "n": _base64url(numbers.n),
        "e": _base64url(numbers.e),
    }

    def serve_jwks(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [jwk]})

    client = httpx.Client(transport=httpx.MockTransport(serve_jwks), follow_redirects=False)
    return JwtJwksVerifier(
        JwtVerifierConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_url=f"{ISSUER}/.well-known/jwks.json",
            algorithms=("RS256",),
        ),
        client=client,
    )


def test_real_jwt_and_case_grant_reauthorization_cycle(database: Database) -> None:
    tenant_id = "oidc-acceptance-tenant"
    case_id = "oidc-case-1"
    run_id = "oidc-run-1"
    subject_id = "oidc-user-1"
    with database.transaction() as connection:
        connection.execute("DELETE FROM aftercare_case_grants WHERE tenant_id=%s", (tenant_id,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant_id,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant_id,))
        RunRepository().create_case(
            connection,
            CaseRecord(tenant_id=tenant_id, case_id=case_id, order_id="oidc-order", version=1),
        )
        RunRepository().create_run(
            connection,
            RunRecord(
                tenant_id=tenant_id,
                case_id=case_id,
                run_id=run_id,
                definition_version="v1",
                input_version=1,
            ),
        )

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private)
    token = _token(private)
    app = create_app(database, oidc_verifier=verifier)
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {token}"}
            denied_without_grant = client.get(f"/v1/cases/{case_id}/runs/{run_id}", headers=headers)
            assert denied_without_grant.status_code == 403

            with database.transaction() as connection:
                grant = CaseGrantRepository().grant(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    case_id=case_id,
                    permissions=("case:read",),
                    granted_by="tenant-admin",
                )

            allowed = client.get(f"/v1/cases/{case_id}/runs/{run_id}", headers=headers)
            assert allowed.status_code == 200

            with database.transaction() as connection:
                CaseGrantRepository().revoke(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    case_id=case_id,
                    revoked_by="tenant-admin",
                    expected_revision=grant.revision,
                )

            denied_after_revoke = client.get(f"/v1/cases/{case_id}/runs/{run_id}", headers=headers)
            assert denied_after_revoke.status_code == 403
    finally:
        verifier.close()


def test_real_jwt_case_grant_controls_strategy_migration(database: Database) -> None:
    tenant_id = "oidc-strategy-tenant"
    case_id = "oidc-strategy-case"
    run_id = "oidc-strategy-run"
    review_id = "oidc-strategy-review"
    subject_id = "oidc-user-1"
    with database.transaction() as connection:
        connection.execute("DELETE FROM aftercare_case_grants WHERE tenant_id=%s", (tenant_id,))
        connection.execute("DELETE FROM aftercare_reviews WHERE tenant_id=%s", (tenant_id,))
        connection.execute("DELETE FROM aftercare_checkpoints WHERE tenant_id=%s", (tenant_id,))
        connection.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant_id,))
        connection.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant_id,))
        RunRepository().create_case(
            connection,
            CaseRecord(
                tenant_id=tenant_id, case_id=case_id, order_id="oidc-strategy-order", version=1
            ),
        )
        RunRepository().create_run(
            connection,
            RunRecord(
                tenant_id=tenant_id,
                case_id=case_id,
                run_id=run_id,
                definition_version="v1",
                input_version=1,
                state="REVIEW",
            ),
        )
        ReviewRepository().request(
            connection,
            ReviewRequest(
                tenant_id=tenant_id,
                case_id=case_id,
                run_id=run_id,
                review_id=review_id,
                reason_code="model-strategy-changed",
                evidence_sha256="a" * 64,
                policy_version="review-v1",
                requested_by="agent-service",
                input_version=1,
            ),
        )
        checkpoint = Checkpoint(
            tenant_id=tenant_id,
            case_id=case_id,
            run_id=run_id,
            checkpoint_version=1,
            input_version=1,
            case_version=1,
            saved_fencing_token=1,
            definition_version="v1",
            policy_version="policy-old",
            tool_schema_version="tools-old",
            model_config_version="model-old",
            strategy_id="strategy-old",
            protocol_version="runtime-v1",
            remaining_budget=RemainingBudget(
                model_calls=1,
                tool_calls=1,
                cost_microusd=100,
                deadline=datetime.now(UTC) + timedelta(minutes=5),
            ),
            next_step="review",
            route_reason="model_strategy_changed",
        )
        connection.execute(
            "INSERT INTO aftercare_checkpoints(tenant_id,case_id,run_id,checkpoint_version,"
            "saved_fencing_token,payload) VALUES (%s,%s,%s,%s,%s,%s)",
            (tenant_id, case_id, run_id, 1, 1, Jsonb(checkpoint.model_dump(mode="json"))),
        )

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _verifier(private)
    token = _token(
        private,
        scope="strategy:migrate",
        tenant_id=tenant_id,
        case_id=case_id,
    )
    app = create_app(database, oidc_verifier=verifier)
    body = {
        "checkpoint_version": 1,
        "old_strategy_id": "strategy-old",
        "old_model_config_version": "model-old",
        "old_policy_version": "policy-old",
        "old_tool_schema_version": "tools-old",
        "new_strategy_id": "strategy-new",
        "new_model_config_version": "model-new",
        "new_policy_version": "policy-new",
        "new_tool_schema_version": "tools-new",
        "reason": "pin reviewed strategy",
    }
    try:
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "oidc-migration-1"}
            denied_without_grant = client.post(
                f"/v1/cases/{case_id}/runs/{run_id}/strategy-migration",
                headers=headers,
                json=body,
            )
            assert denied_without_grant.status_code == 403

            with database.transaction() as connection:
                grant = CaseGrantRepository().grant(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    case_id=case_id,
                    permissions=("strategy:migrate",),
                    granted_by="tenant-admin",
                )
            allowed = client.post(
                f"/v1/cases/{case_id}/runs/{run_id}/strategy-migration",
                headers=headers,
                json=body,
            )
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["checkpoint_version_after"] == 2

            with database.transaction() as connection:
                CaseGrantRepository().revoke(
                    connection,
                    tenant_id=tenant_id,
                    subject_id=subject_id,
                    case_id=case_id,
                    revoked_by="tenant-admin",
                    expected_revision=grant.revision,
                )
            denied_after_revoke = client.post(
                f"/v1/cases/{case_id}/runs/{run_id}/strategy-migration",
                headers=headers,
                json=body,
            )
            assert denied_after_revoke.status_code == 403
    finally:
        verifier.close()
