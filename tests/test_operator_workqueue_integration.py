"""PostgreSQL API checks for operator Case discovery and work-queue reads."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aftercare_agent.actions import ActionIntent
from aftercare_agent.api.app import create_app
from aftercare_agent.domain.approvals import ApprovalRequest
from aftercare_agent.domain.reviews import ReviewRequest
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.persistence import (
    ActionRepository,
    ApprovalRepository,
    Database,
    ReviewRepository,
    RunRepository,
    migrate,
)


@pytest.fixture()
def database() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


@pytest.fixture()
def client(database: Database) -> Iterator[TestClient]:
    with TestClient(create_app(database, allow_synthetic=True)) as value:
        yield value


def _open_case(
    database: Database, *, case_status: str = "OPEN", run_state: str = "READY"
) -> tuple[str, str, str]:
    tenant = f"queue-{uuid4().hex[:12]}"
    case_id = "case-1"
    run_id = "run-1"
    with database.transaction() as connection:
        runs = RunRepository()
        runs.create_case(
            connection,
            CaseRecord(
                tenant_id=tenant,
                case_id=case_id,
                order_id="order-1",
                version=1,
                status=case_status,  # type: ignore[arg-type]
            ),
        )
        runs.create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                definition_version="v1",
                input_version=1,
                state=run_state,  # type: ignore[arg-type]
            ),
        )
    return tenant, case_id, run_id


def test_case_queue_lists_case_detail_and_empty_children(
    database: Database, client: TestClient
) -> None:
    tenant, case_id, run_id = _open_case(database)
    headers = {"X-Synthetic-Tenant": tenant, "X-Synthetic-Subject": "ops-1"}

    listed = client.get("/v1/cases", headers=headers)
    assert listed.status_code == 200
    body = listed.json()
    assert [item["case_id"] for item in body["cases"]] == [case_id]
    assert "case:read" in body["cases"][0]["permissions"]
    assert "tenant_id" not in body["cases"][0]
    # A short page carries no cursor, so the caller knows it reached the end.
    assert body["next_created_at"] is None and body["next_case_id"] is None

    detail = client.get(f"/v1/cases/{case_id}", headers=headers)
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["order_id"] == "order-1"
    assert detail_body["status"] == "OPEN"
    assert "tenant_id" not in detail_body
    assert [run["run_id"] for run in detail_body["runs"]] == [run_id]
    # Run internals (tenant, lease owner, fencing token) stay server-side.
    assert "tenant_id" not in detail_body["runs"][0]
    assert "lease_owner" not in detail_body["runs"][0]
    assert "fencing_token" not in detail_body["runs"][0]

    reviews = client.get(f"/v1/cases/{case_id}/reviews", headers=headers)
    assert reviews.status_code == 200 and reviews.json() == {"reviews": []}

    approvals = client.get(f"/v1/cases/{case_id}/approvals", headers=headers)
    assert approvals.status_code == 200 and approvals.json() == {"approvals": []}

    filtered = client.get("/v1/cases", params={"status": "CLOSED"}, headers=headers)
    assert filtered.status_code == 200 and filtered.json()["cases"] == []


def test_case_queue_lists_pending_review_without_internal_fields(
    database: Database, client: TestClient
) -> None:
    tenant, case_id, run_id = _open_case(database, run_state="REVIEW")
    with database.transaction() as connection:
        ReviewRepository().request(
            connection,
            ReviewRequest(
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                review_id="review-1",
                reason_code="evidence-conflict",
                evidence_sha256="a" * 64,
                policy_version="review-v1",
                requested_by="agent-service",
                input_version=1,
            ),
        )
    headers = {"X-Synthetic-Tenant": tenant, "X-Synthetic-Subject": "ops-reviewer"}

    listed = client.get(f"/v1/cases/{case_id}/reviews", headers=headers)
    assert listed.status_code == 200
    reviews = listed.json()["reviews"]
    assert [item["review_id"] for item in reviews] == ["review-1"]
    assert reviews[0]["decision"] is None
    assert "evidence_sha256" not in reviews[0]
    assert "policy_version" not in reviews[0]
    assert "tenant_id" not in reviews[0]


def test_case_queue_lists_pending_approval_without_internal_fields(
    database: Database, client: TestClient
) -> None:
    tenant, case_id, _ = _open_case(database)
    with database.transaction() as connection:
        ActionRepository().reserve(
            connection,
            ActionIntent(
                tenant_id=tenant,
                case_id=case_id,
                order_id="order-1",
                action_id="action-1",
                action_type="refund",
                business_key=f"payment:{uuid4().hex}:refund",
                idempotency_key=f"request:{uuid4().hex}",
                parameters_sha256="b" * 64,
                amount_minor="2500",
                currency="USD",
                provider_idempotency_key=f"refund:{uuid4().hex}",
            ),
        )
        ApprovalRepository().request(
            connection,
            ApprovalRequest(
                tenant_id=tenant,
                case_id=case_id,
                approval_id="approval-1",
                action_id="action-1",
                action_parameters_sha256="b" * 64,
                policy_version="refund-v1",
                requested_by="agent-service",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
        )
    headers = {"X-Synthetic-Tenant": tenant, "X-Synthetic-Subject": "ops-approver"}

    listed = client.get(f"/v1/cases/{case_id}/approvals", headers=headers)
    assert listed.status_code == 200
    approvals = listed.json()["approvals"]
    assert [item["approval_id"] for item in approvals] == ["approval-1"]
    assert approvals[0]["decision"] == "PENDING"
    assert "action_parameters_sha256" not in approvals[0]
    assert "policy_version" not in approvals[0]
    assert "tenant_id" not in approvals[0]


def test_case_queue_is_tenant_scoped(database: Database, client: TestClient) -> None:
    _tenant_a, case_id, _ = _open_case(database)
    other_tenant = f"queue-{uuid4().hex[:12]}"
    headers = {"X-Synthetic-Tenant": other_tenant, "X-Synthetic-Subject": "ops-other"}

    listed = client.get("/v1/cases", headers=headers)
    assert listed.status_code == 200 and listed.json()["cases"] == []

    detail = client.get(f"/v1/cases/{case_id}", headers=headers)
    assert detail.status_code == 403

    reviews = client.get(f"/v1/cases/{case_id}/reviews", headers=headers)
    assert reviews.status_code == 403


def test_case_queue_requires_authentication(database: Database) -> None:
    headers = {"X-Synthetic-Tenant": "queue-auth", "X-Synthetic-Subject": "ops"}
    with TestClient(create_app(database)) as production_client:
        listed = production_client.get("/v1/cases", headers=headers)
        detail = production_client.get("/v1/cases/case-1", headers=headers)
    assert listed.status_code == 401
    assert detail.status_code == 401
