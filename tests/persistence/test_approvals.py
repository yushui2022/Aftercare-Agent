"""PostgreSQL approval-gate tests with no external identity provider."""

import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from aftercare_agent.actions import ActionIntent
from aftercare_agent.domain.approvals import ApprovalRequest
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import CaseRecord
from aftercare_agent.persistence import (
    ActionRepository,
    ApprovalRepository,
    Database,
    RunRepository,
    migrate,
)


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


def _seed(db: Database, tenant: str = "approval-test") -> ActionIntent:
    with db.transaction() as conn:
        conn.execute("DELETE FROM aftercare_actions WHERE tenant_id=%s", (tenant,))
        conn.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        conn.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        RunRepository().create_case(
            conn,
            CaseRecord(tenant_id=tenant, case_id="case-1", order_id="order-1", version=1),
        )
        intent = ActionIntent(
            tenant_id=tenant,
            case_id="case-1",
            order_id="order-1",
            action_id="action-1",
            action_type="refund",
            business_key="payment:p1:refund:r1",
            idempotency_key="request-1",
            parameters_sha256="a" * 64,
            amount_minor="2500",
            currency="USD",
            provider_idempotency_key="refund:r1",
        )
        ActionRepository().reserve(conn, intent)
        return intent


def _request(intent: ActionIntent, *, expires: timedelta = timedelta(minutes=5)) -> ApprovalRequest:
    return ApprovalRequest(
        tenant_id=intent.tenant_id,
        case_id=intent.case_id,
        approval_id="approval-1",
        action_id=intent.action_id,
        action_parameters_sha256=intent.parameters_sha256,
        policy_version="refund-v1",
        requested_by="agent-service",
        expires_at=datetime.now(UTC) + expires,
    )


def test_decision_is_bound_and_idempotent(db: Database) -> None:
    intent = _seed(db)
    repository = ApprovalRepository()
    request = _request(intent)
    with db.transaction() as conn:
        first, replayed = repository.request(conn, request)
        assert first.decision == "PENDING" and not replayed
        approved = repository.decide(
            conn,
            intent.tenant_id,
            request.approval_id,
            approver="ops-reviewer",
            decision="APPROVED",
            decision_idempotency_key="decision-1",
            decision_reason="policy matched",
        )
        assert approved.decision == "APPROVED"
        assert (
            repository.decide(
                conn,
                intent.tenant_id,
                request.approval_id,
                approver="ops-reviewer",
                decision="APPROVED",
                decision_idempotency_key="decision-1",
                decision_reason="policy matched",
            )
            == approved
        )
        with pytest.raises(ContractViolation) as error:
            repository.decide(
                conn,
                intent.tenant_id,
                request.approval_id,
                approver="other-reviewer",
                decision="REJECTED",
                decision_idempotency_key="decision-2",
            )
        assert error.value.code is ErrorCode.CONFLICT


def test_requester_cannot_self_approve_or_approve_expired_request(db: Database) -> None:
    intent = _seed(db, "approval-restrictions")
    repository = ApprovalRepository()
    request = _request(intent, expires=timedelta(milliseconds=100))
    with db.transaction() as conn:
        repository.request(conn, request)
        with pytest.raises(ContractViolation) as self_approval:
            repository.decide(
                conn,
                intent.tenant_id,
                request.approval_id,
                approver="agent-service",
                decision="APPROVED",
                decision_idempotency_key="self-decision",
            )
        assert self_approval.value.code is ErrorCode.FORBIDDEN
        time.sleep(0.2)
        with pytest.raises(ContractViolation) as expired:
            repository.decide(
                conn,
                intent.tenant_id,
                request.approval_id,
                approver="ops-reviewer",
                decision="APPROVED",
                decision_idempotency_key="expired-decision",
            )
        assert expired.value.code is ErrorCode.FORBIDDEN
        assert repository.expire(conn, intent.tenant_id, request.approval_id).decision == "EXPIRED"


def test_dispatch_rechecks_policy_and_digest(db: Database) -> None:
    intent = _seed(db, "approval-dispatch")
    repository = ApprovalRepository()
    request = _request(intent)
    with db.transaction() as conn:
        repository.request(conn, request)
        repository.decide(
            conn,
            intent.tenant_id,
            request.approval_id,
            approver="ops-reviewer",
            decision="APPROVED",
            decision_idempotency_key="decision-dispatch",
        )
        with pytest.raises(ContractViolation) as policy:
            repository.lock_for_dispatch(
                conn,
                tenant_id=intent.tenant_id,
                case_id=intent.case_id,
                action_id=intent.action_id,
                approval_id=request.approval_id,
                action_parameters_sha256=intent.parameters_sha256,
                policy_version="refund-v2",
            )
        assert policy.value.code is ErrorCode.CONFLICT
        with pytest.raises(ContractViolation) as digest:
            repository.lock_for_dispatch(
                conn,
                tenant_id=intent.tenant_id,
                case_id=intent.case_id,
                action_id=intent.action_id,
                approval_id=request.approval_id,
                action_parameters_sha256="b" * 64,
                policy_version="refund-v1",
            )
        assert digest.value.code is ErrorCode.CONFLICT
