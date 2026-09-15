"""PostgreSQL API checks for the operator Review/Approval control plane."""

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
)


@pytest.fixture()
def client(db: Database) -> Iterator[TestClient]:
    with TestClient(create_app(db, allow_synthetic=True)) as value:
        yield value


def _review_fixture(db: Database) -> tuple[str, str, str]:
    tenant = f"operator-review-{uuid4().hex[:12]}"
    case_id = "case-1"
    run_id = "run-1"
    review_id = "review-1"
    with db.transaction() as connection:
        runs = RunRepository()
        runs.create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id=case_id, order_id="order-1", version=1),
        )
        runs.create_run(
            connection,
            RunRecord(
                tenant_id=tenant,
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
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                review_id=review_id,
                reason_code="evidence-conflict",
                evidence_sha256="a" * 64,
                policy_version="review-v1",
                requested_by="agent-service",
                input_version=1,
            ),
        )
    return tenant, case_id, review_id


def _approval_fixture(db: Database) -> tuple[str, str, str]:
    tenant = f"operator-approval-{uuid4().hex[:12]}"
    case_id = "case-1"
    action_id = "action-1"
    approval_id = "approval-1"
    with db.transaction() as connection:
        RunRepository().create_case(
            connection,
            CaseRecord(tenant_id=tenant, case_id=case_id, order_id="order-1", version=1),
        )
        ActionRepository().reserve(
            connection,
            ActionIntent(
                tenant_id=tenant,
                case_id=case_id,
                order_id="order-1",
                action_id=action_id,
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
                approval_id=approval_id,
                action_id=action_id,
                action_parameters_sha256="b" * 64,
                policy_version="refund-v1",
                requested_by="agent-service",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
        )
    return tenant, case_id, approval_id


def test_review_operator_routes_derive_reviewer_and_are_idempotent(
    db: Database, client: TestClient
) -> None:
    tenant, case_id, review_id = _review_fixture(db)
    headers = {"X-Synthetic-Tenant": tenant, "X-Synthetic-Subject": "ops-reviewer"}

    fetched = client.get(f"/v1/cases/{case_id}/reviews/{review_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["decision"] is None
    assert "tenant_id" not in fetched.json()

    decided = client.post(
        f"/v1/cases/{case_id}/reviews/{review_id}/decision",
        headers={**headers, "Idempotency-Key": "review-decision-1"},
        json={"decision": "CONTINUE", "decision_reason": "evidence accepted"},
    )
    assert decided.status_code == 200
    assert decided.json()["actor"] == "ops-reviewer"
    assert "tenant_id" not in decided.json()
    assert "evidence_sha256" not in decided.json()
    assert "policy_version" not in decided.json()
    assert decided.json()["decision"] == "CONTINUE"

    replay = client.post(
        f"/v1/cases/{case_id}/reviews/{review_id}/decision",
        headers={**headers, "Idempotency-Key": "review-decision-1"},
        json={"decision": "CONTINUE", "decision_reason": "evidence accepted"},
    )
    assert replay.status_code == 200
    assert replay.json()["actor"] == "ops-reviewer"

    changed_decision = client.post(
        f"/v1/cases/{case_id}/reviews/{review_id}/decision",
        headers={**headers, "Idempotency-Key": "review-decision-2"},
        json={"decision": "CANCEL"},
    )
    assert changed_decision.status_code == 409

    missing_key = client.post(
        f"/v1/cases/{case_id}/reviews/{review_id}/decision",
        headers=headers,
        json={"decision": "CONTINUE"},
    )
    assert missing_key.status_code == 400

    authority_in_body = client.post(
        f"/v1/cases/{case_id}/reviews/{review_id}/decision",
        headers={**headers, "Idempotency-Key": "review-decision-2"},
        json={"decision": "CONTINUE", "reviewer": "attacker"},
    )
    assert authority_in_body.status_code == 422


def test_approval_operator_routes_derive_approver_and_enforce_case_scope(
    db: Database, client: TestClient
) -> None:
    tenant, case_id, approval_id = _approval_fixture(db)
    headers = {"X-Synthetic-Tenant": tenant, "X-Synthetic-Subject": "ops-approver"}

    fetched = client.get(f"/v1/cases/{case_id}/approvals/{approval_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["decision"] == "PENDING"
    assert fetched.json()["run_id"] is None
    assert "tenant_id" not in fetched.json()

    decided = client.post(
        f"/v1/cases/{case_id}/approvals/{approval_id}/decision",
        headers={**headers, "Idempotency-Key": "approval-decision-1"},
        json={"decision": "APPROVED", "decision_reason": "policy matched"},
    )
    assert decided.status_code == 200
    assert decided.json()["actor"] == "ops-approver"
    assert "tenant_id" not in decided.json()
    assert "action_parameters_sha256" not in decided.json()
    assert "policy_version" not in decided.json()
    assert decided.json()["decision"] == "APPROVED"

    changed_decision = client.post(
        f"/v1/cases/{case_id}/approvals/{approval_id}/decision",
        headers={**headers, "Idempotency-Key": "approval-decision-2"},
        json={"decision": "REJECTED"},
    )
    assert changed_decision.status_code == 409

    wrong_case = client.get(f"/v1/cases/other-case/approvals/{approval_id}", headers=headers)
    assert wrong_case.status_code == 403

    authority_in_body = client.post(
        f"/v1/cases/{case_id}/approvals/{approval_id}/decision",
        headers={**headers, "Idempotency-Key": "approval-decision-2"},
        json={"decision": "APPROVED", "approver": "attacker"},
    )
    assert authority_in_body.status_code == 422


def test_operator_routes_reject_synthetic_identity_when_disabled(db: Database) -> None:
    with TestClient(create_app(db)) as production_client:
        response = production_client.get(
            "/v1/cases/case-1/reviews/review-1",
            headers={
                "X-Synthetic-Tenant": "operator-review",
                "X-Synthetic-Subject": "operator",
            },
        )
    assert response.status_code == 401
