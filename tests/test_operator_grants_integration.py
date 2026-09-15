"""PostgreSQL API checks for the Case-grant administration surface.

These exercise the dead end the surface exists to remove: before a grant, an
operator who holds the right scopes still cannot touch someone else's Case.
Every identity here is a bearer identity with an explicit scope set, so the
delegation bound is actually tested rather than masked by the local test
identity.
"""

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aftercare_agent.api.app import create_app
from aftercare_agent.auth import AuthContext
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.reviews import ReviewRequest
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.persistence import (
    Database,
    EventRepository,
    ReviewRepository,
    RunRepository,
    migrate,
)

CASE_ID = "case-1"
REVIEW_ID = "review-1"


@pytest.fixture()
def database() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


class _BearerVerifier:
    """Seam double mapping tokens to identities; JWKS behavior is covered separately."""

    def __init__(self, identities: dict[str, AuthContext]) -> None:
        self._identities = identities

    def verify(self, authorization: str, *, now: datetime | None = None) -> AuthContext:
        token = authorization.removeprefix("Bearer ")
        identity = self._identities.get(token)
        if identity is None or not authorization.startswith("Bearer "):
            raise ContractViolation(ErrorCode.UNAUTHENTICATED, "invalid bearer token")
        return identity


def _identity(subject_id: str, tenant_id: str, permissions: set[str]) -> AuthContext:
    return AuthContext(
        subject_id=subject_id, tenant_id=tenant_id, permissions=frozenset(permissions)
    )


def _client(database: Database, tenant: str) -> TestClient:
    """One app, four identities: administration, a limited administrator, two operators."""
    verifier = _BearerVerifier(
        {
            "admin-token": _identity(
                "grant-admin",
                tenant,
                {"grant:read", "grant:admin", "case:read", "review:read", "review:decide"},
            ),
            "limited-token": _identity(
                "limited-admin", tenant, {"grant:read", "grant:admin", "case:read"}
            ),
            "operator-token": _identity(
                "operator-1", tenant, {"case:read", "review:read", "review:decide"}
            ),
            "reader-token": _identity("case-reader", tenant, {"case:read"}),
        }
    )
    return TestClient(create_app(database, oidc_verifier=verifier))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _case_with_review(database: Database) -> str:
    """A tenant whose Case already has a pending Review for the handover test."""
    tenant = f"grant-admin-{uuid4().hex[:12]}"
    with database.transaction() as connection:
        runs = RunRepository()
        runs.create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id=CASE_ID, order_id="order-1", version=1),
        )
        runs.create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
                case_id=CASE_ID,
                run_id="run-1",
                definition_version="v1",
                input_version=1,
                state="REVIEW",
            ),
        )
        ReviewRepository().request(
            connection,
            ReviewRequest(
                tenant_id=tenant,
                case_id=CASE_ID,
                run_id="run-1",
                review_id=REVIEW_ID,
                reason_code="evidence-conflict",
                evidence_sha256="a" * 64,
                policy_version="review-v1",
                requested_by="agent-service",
                input_version=1,
            ),
        )
    return tenant


def _grant(client: TestClient, token: str, **body: object) -> Any:
    payload: dict[str, object] = {
        "subject_id": "operator-1",
        "permissions": ["case:read", "review:read", "review:decide"],
    }
    payload.update(body)
    return client.post(f"/v1/cases/{CASE_ID}/grants", headers=_bearer(token), json=payload)


def test_a_granted_operator_can_take_over_another_subjects_case(database: Database) -> None:
    tenant = _case_with_review(database)
    client = _client(database, tenant)

    # Scopes alone are not authority: without a CaseGrant the Case is invisible.
    assert client.get(f"/v1/cases/{CASE_ID}", headers=_bearer("operator-token")).status_code == 403
    assert client.get("/v1/cases", headers=_bearer("operator-token")).json()["cases"] == []

    granted = _grant(client, "admin-token")
    assert granted.status_code == 200
    assert granted.json()["subject_id"] == "operator-1"
    assert granted.json()["granted_by"] == "grant-admin"
    assert granted.json()["revision"] == 1
    assert "tenant_id" not in granted.json()

    listing = client.get("/v1/cases", headers=_bearer("operator-token")).json()["cases"]
    assert [entry["case_id"] for entry in listing] == [CASE_ID]
    assert listing[0]["permissions"] == ["case:read", "review:decide", "review:read"]

    review = client.get(
        f"/v1/cases/{CASE_ID}/reviews/{REVIEW_ID}", headers=_bearer("operator-token")
    )
    assert review.status_code == 200
    decided = client.post(
        f"/v1/cases/{CASE_ID}/reviews/{REVIEW_ID}/decision",
        headers={**_bearer("operator-token"), "Idempotency-Key": "handover-decision-1"},
        json={"decision": "CONTINUE", "decision_reason": "handed over"},
    )
    assert decided.status_code == 200
    assert decided.json()["actor"] == "operator-1"


def test_revoking_a_grant_closes_access_again(database: Database) -> None:
    tenant = _case_with_review(database)
    client = _client(database, tenant)
    revision = _grant(client, "admin-token").json()["revision"]

    revoked = client.post(
        f"/v1/cases/{CASE_ID}/grants/operator-1/revoke",
        headers=_bearer("admin-token"),
        json={"expected_revision": revision},
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_by"] == "grant-admin"
    assert revoked.json()["revoked_at"] is not None
    assert revoked.json()["revision"] == revision + 1

    assert client.get(f"/v1/cases/{CASE_ID}", headers=_bearer("operator-token")).status_code == 403
    assert client.get("/v1/cases", headers=_bearer("operator-token")).json()["cases"] == []


def test_replacing_a_grant_requires_the_current_revision(database: Database) -> None:
    tenant = _case_with_review(database)
    client = _client(database, tenant)
    assert _grant(client, "admin-token").status_code == 200

    # A blind retry cannot silently apply twice.
    assert _grant(client, "admin-token").status_code == 409
    assert _grant(client, "admin-token", expected_revision=99).status_code == 409

    replaced = _grant(client, "admin-token", permissions=["case:read"], expected_revision=1)
    assert replaced.status_code == 200
    assert replaced.json()["permissions"] == ["case:read"]
    assert replaced.json()["revision"] == 2

    listed = client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token")).json()
    assert len(listed["grants"]) == 1
    assert listed["grants"][0]["revision"] == 2


def test_only_grant_administrators_may_read_or_change_access(database: Database) -> None:
    tenant = _case_with_review(database)
    client = _client(database, tenant)

    assert (
        client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("reader-token")).status_code
        == 403
    )
    assert _grant(client, "reader-token").status_code == 403
    revoke = client.post(
        f"/v1/cases/{CASE_ID}/grants/operator-1/revoke",
        headers=_bearer("reader-token"),
        json={"expected_revision": 1},
    )
    assert revoke.status_code == 403
    # The reader holds grant:read only for its own tenant, so it still cannot
    # administer, and nothing was written.
    assert client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token")).json() == {
        "grants": []
    }


def test_an_administrator_cannot_delegate_beyond_its_own_permissions(database: Database) -> None:
    tenant = _case_with_review(database)
    client = _client(database, tenant)

    denied = _grant(client, "limited-token", permissions=["review:decide"])
    assert denied.status_code == 403
    assert client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("limited-token")).json() == {
        "grants": []
    }
    # The same administrator can still delegate what it does hold.
    assert _grant(client, "limited-token", permissions=["case:read"]).status_code == 200


def test_an_unknown_permission_never_reaches_the_acl(database: Database) -> None:
    tenant = _case_with_review(database)
    client = _client(database, tenant)

    rejected = _grant(client, "admin-token", permissions=["approval:approve"])
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "invalid_input"
    assert client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token")).json() == {
        "grants": []
    }


def test_an_expiry_that_is_not_in_the_future_is_rejected(database: Database) -> None:
    tenant = _case_with_review(database)
    client = _client(database, tenant)

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    rejected = _grant(client, "admin-token", expires_at=past)
    assert rejected.status_code == 400

    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert _grant(client, "admin-token", expires_at=future).status_code == 200


def test_another_tenants_case_is_indistinguishable(database: Database) -> None:
    tenant = _case_with_review(database)
    client = _client(database, "some-other-tenant")

    assert (
        client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token")).status_code == 403
    )
    assert _grant(client, "admin-token").status_code == 403
    assert _grant(client, "admin-token").json()["detail"] == "forbidden"
    assert (
        client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token")).status_code == 403
    )
    # The owning tenant is unaffected.
    assert _client(database, tenant).get(
        f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token")
    ).json() == {"grants": []}


def test_access_changes_appear_in_the_case_stream(database: Database) -> None:
    tenant = _case_with_review(database)
    client = _client(database, tenant)
    with database.transaction() as connection:
        history = [
            event.event_type
            for event in EventRepository().list_case_events(
                connection, tenant_id=tenant, case_id=CASE_ID
            )
        ]
    revision = _grant(client, "admin-token").json()["revision"]
    client.post(
        f"/v1/cases/{CASE_ID}/grants/operator-1/revoke",
        headers=_bearer("admin-token"),
        json={"expected_revision": revision},
    )

    with database.transaction() as connection:
        events = EventRepository().list_case_events(connection, tenant_id=tenant, case_id=CASE_ID)
        applied = [event.event_type for event in events]
        sequences = [event.case_seq for event in events]
        payloads = connection.execute(
            "SELECT count(*) FROM aftercare_event_payloads WHERE tenant_id=%s AND case_id=%s",
            (tenant, CASE_ID),
        ).fetchone()
    # The access changes append to the Case history the grant did not create.
    assert applied[: len(history)] == history
    assert applied[len(history) :] == ["case_grant.granted", "case_grant.revoked"]
    assert sequences == list(range(1, len(events) + 1))
    # Each change keeps an immutable snapshot, not just a reference.
    assert payloads == (len(events),)


def test_a_granted_operator_can_read_the_access_event_in_the_api_stream(
    database: Database,
) -> None:
    tenant = _case_with_review(database)
    client = _client(database, tenant)
    assert _grant(client, "admin-token").status_code == 200

    stream = client.get(f"/v1/cases/{CASE_ID}/events", headers=_bearer("operator-token"))
    assert stream.status_code == 200
    assert "event: case_grant.granted" in stream.text
    assert "case_grant.revoked" not in stream.text
