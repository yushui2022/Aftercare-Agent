"""PostgreSQL API checks for the Case-grant administration surface.

These exercise the dead end the surface exists to remove: before a grant, an
operator who holds the right scopes still cannot touch someone else's Case.
Every identity here is a bearer identity with an explicit scope set, so the
delegation bound is actually tested rather than masked by the local test
identity.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

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
)

CASE_ID = "case-1"
REVIEW_ID = "review-1"


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


def _client(db: Database, tenant: str) -> TestClient:
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
    return TestClient(create_app(db, oidc_verifier=verifier))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _case_with_review(db: Database) -> str:
    """A tenant whose Case already has a pending Review for the handover test."""
    tenant = f"grant-admin-{uuid4().hex[:12]}"
    with db.transaction() as connection:
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


def test_a_granted_operator_can_take_over_another_subjects_case(db: Database) -> None:
    tenant = _case_with_review(db)
    client = _client(db, tenant)

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


def test_revoking_a_grant_closes_access_again(db: Database) -> None:
    tenant = _case_with_review(db)
    client = _client(db, tenant)
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


def test_replacing_a_grant_requires_the_current_revision(db: Database) -> None:
    tenant = _case_with_review(db)
    client = _client(db, tenant)
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


def test_only_grant_administrators_may_read_or_change_access(db: Database) -> None:
    tenant = _case_with_review(db)
    client = _client(db, tenant)

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
    listed = client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token")).json()
    assert listed["grants"] == []


def test_an_administrator_cannot_delegate_beyond_its_own_permissions(db: Database) -> None:
    tenant = _case_with_review(db)
    client = _client(db, tenant)

    denied = _grant(client, "limited-token", permissions=["review:decide"])
    assert denied.status_code == 403
    listed = client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("limited-token")).json()
    # Rejecting the delegation changed nothing, and the caller is told exactly
    # what it may still hand out.
    assert listed["grants"] == []
    assert listed["delegable"] == ["case:read"]
    assert listed["can_administer"] is True
    # The same administrator can still delegate what it does hold.
    assert _grant(client, "limited-token", permissions=["case:read"]).status_code == 200


def test_an_unknown_permission_never_reaches_the_acl(db: Database) -> None:
    tenant = _case_with_review(db)
    client = _client(db, tenant)

    rejected = _grant(client, "admin-token", permissions=["approval:approve"])
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "invalid_input"
    listed = client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token")).json()
    assert listed["grants"] == []


def test_an_expiry_that_is_not_in_the_future_is_rejected(db: Database) -> None:
    tenant = _case_with_review(db)
    client = _client(db, tenant)

    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    rejected = _grant(client, "admin-token", expires_at=past)
    assert rejected.status_code == 400

    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert _grant(client, "admin-token", expires_at=future).status_code == 200


def test_another_tenants_case_is_indistinguishable(db: Database) -> None:
    tenant = _case_with_review(db)
    client = _client(db, "some-other-tenant")

    assert (
        client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token")).status_code == 403
    )
    assert _grant(client, "admin-token").status_code == 403
    assert _grant(client, "admin-token").json()["detail"] == "forbidden"
    assert (
        client.get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token")).status_code == 403
    )
    # The owning tenant is unaffected.
    owner_view = (
        _client(db, tenant)
        .get(f"/v1/cases/{CASE_ID}/grants", headers=_bearer("admin-token"))
        .json()
    )
    assert owner_view["grants"] == []


def test_access_changes_appear_in_the_case_stream(db: Database) -> None:
    tenant = _case_with_review(db)
    client = _client(db, tenant)
    with db.transaction() as connection:
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

    with db.transaction() as connection:
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
    db: Database,
) -> None:
    tenant = _case_with_review(db)
    client = _client(db, tenant)
    assert _grant(client, "admin-token").status_code == 200

    stream = client.get(f"/v1/cases/{CASE_ID}/events", headers=_bearer("operator-token"))
    assert stream.status_code == 200
    assert "event: case_grant.granted" in stream.text
    assert "case_grant.revoked" not in stream.text


def _administration_client(db: Database, tenant: str, case_ids: set[str]) -> TestClient:
    """One administrator whose token is narrowed to ``case_ids``."""
    verifier = _BearerVerifier(
        {
            "narrow-token": replace(
                _identity(
                    "grant-admin",
                    tenant,
                    {"grant:read", "grant:admin", "case:read", "review:read", "review:decide"},
                ),
                case_ids=frozenset(case_ids),
            )
        }
    )
    return TestClient(create_app(db, oidc_verifier=verifier))


def _create_case(db: Database, tenant: str, case_id: str, order_id: str) -> None:
    with db.transaction() as connection:
        RunRepository().create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id=case_id, order_id=order_id, version=1),
        )


def test_an_access_administrator_can_discover_a_case_it_does_not_hold(
    db: Database,
) -> None:
    """A first grant is impossible if the administrator cannot name the Case.

    The Case below is granted to nobody, so the administrator's own operator
    queue stays empty while the control-plane inventory has to show it.  That
    inventory carries identifiers and progress, never Case content.
    """
    tenant = _case_with_review(db)
    client = _client(db, tenant)

    assert client.get("/v1/cases", headers=_bearer("admin-token")).json()["cases"] == []

    inventory = client.get("/v1/administration/cases", headers=_bearer("admin-token"))
    assert inventory.status_code == 200
    body = inventory.json()
    assert [entry["case_id"] for entry in body["cases"]] == [CASE_ID]
    assert body["cases"][0]["order_id"] == "order-1"
    # Administration scopes, and nothing that claims Case content: the token
    # lists case:read, but without a grant there is no Case permission here.
    assert body["cases"][0]["permissions"] == ["grant:admin", "grant:read"]

    # The inventory delegates discovery only.  Content still resolves through
    # a per-Case grant, so the administrator reads neither the header nor the
    # Review it is about to hand over.
    assert client.get(f"/v1/cases/{CASE_ID}", headers=_bearer("admin-token")).status_code == 403
    assert (
        client.get(
            f"/v1/cases/{CASE_ID}/reviews/{REVIEW_ID}", headers=_bearer("admin-token")
        ).status_code
        == 403
    )


def test_only_grant_readers_can_enumerate_the_tenant(db: Database) -> None:
    tenant = _case_with_review(db)
    client = _client(db, tenant)

    # Holding Case scopes is not enough: without grant:read the tenant's Case
    # list is not a resource this identity may enumerate at all.
    for token in ("operator-token", "reader-token"):
        assert client.get("/v1/administration/cases", headers=_bearer(token)).status_code == 403


def test_the_administration_inventory_stays_inside_the_tenant(db: Database) -> None:
    tenant = _case_with_review(db)
    # The same case id in another tenant must not surface.
    _create_case(db, f"other-{uuid4().hex[:12]}", CASE_ID, "order-other-tenant")
    client = _client(db, tenant)

    body = client.get("/v1/administration/cases", headers=_bearer("admin-token")).json()
    assert [entry["order_id"] for entry in body["cases"]] == ["order-1"]


def test_a_narrowed_administration_token_cannot_widen_the_inventory(
    db: Database,
) -> None:
    tenant = _case_with_review(db)
    _create_case(db, tenant, "case-2", "order-2")
    client = _administration_client(db, tenant, {"case-2"})

    body = client.get("/v1/administration/cases", headers=_bearer("narrow-token")).json()
    assert [entry["case_id"] for entry in body["cases"]] == ["case-2"]


def test_the_administration_inventory_pages_with_a_keyset_cursor(
    db: Database,
) -> None:
    tenant = _case_with_review(db)
    _create_case(db, tenant, "case-2", "order-2")
    client = _client(db, tenant)

    first = client.get(
        "/v1/administration/cases", headers=_bearer("admin-token"), params={"limit": 1}
    ).json()
    assert len(first["cases"]) == 1
    assert first["next_case_id"] == first["cases"][0]["case_id"]
    rest = client.get(
        "/v1/administration/cases",
        headers=_bearer("admin-token"),
        params={
            "limit": 1,
            "after_created_at": first["next_created_at"],
            "after_case_id": first["next_case_id"],
        },
    ).json()
    assert {entry["case_id"] for entry in rest["cases"]}.isdisjoint(
        {entry["case_id"] for entry in first["cases"]}
    )
