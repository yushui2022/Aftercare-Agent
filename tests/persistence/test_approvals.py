"""PostgreSQL approval-gate tests with no external identity provider."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from aftercare_agent.actions import ActionIntent
from aftercare_agent.domain.approvals import ApprovalRequest
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.domain.waits import WaitRecord
from aftercare_agent.persistence import (
    ActionRepository,
    ApprovalRepository,
    Database,
    RunRepository,
    WaitRepository,
)


def _seed(db: Database, tenant: str = "approval-test") -> ActionIntent:
    with db.transaction() as conn:
        conn.execute("DELETE FROM aftercare_actions WHERE tenant_id=%s", (tenant,))
        conn.execute("DELETE FROM aftercare_wait_wakeups WHERE tenant_id=%s", (tenant,))
        conn.execute("DELETE FROM aftercare_waits WHERE tenant_id=%s", (tenant,))
        conn.execute("DELETE FROM aftercare_inbox WHERE tenant_id=%s", (tenant,))
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


def _seed_bound(db: Database, tenant: str) -> tuple[ActionIntent, WaitRecord]:
    intent = _seed(db, tenant)
    now = datetime.now(UTC)
    wait = WaitRecord(
        tenant_id=tenant,
        case_id="case-1",
        run_id="run-1",
        wait_id="approval-wait-1",
        generation=1,
        kind="approval",
        correlation_key="approval-1",
        condition_version=intent.parameters_sha256,
        created_at=now - timedelta(seconds=2),
        deadline=now + timedelta(minutes=5),
        state="PENDING",
    )
    with db.transaction() as conn:
        runs = RunRepository()
        runs.create_run(
            conn,
            RunRecord(
                tenant_id=tenant,
                case_id="case-1",
                run_id="run-1",
                definition_version="v1",
                input_version=1,
            ),
        )
        claim = runs.claim(conn, tenant, "run-1", "worker-a", timedelta(seconds=30))
        WaitRepository().register(conn, wait, claim)
        runs.transition(
            conn,
            claim,
            "WAITING_APPROVAL",
            wait_id=wait.wait_id,
            wait_generation=wait.generation,
        )
        WaitRepository().activate(conn, wait)
    return intent, wait


def _bound_request(intent: ActionIntent, wait: WaitRecord) -> ApprovalRequest:
    return ApprovalRequest(
        tenant_id=intent.tenant_id,
        case_id=intent.case_id,
        approval_id="approval-1",
        action_id=intent.action_id,
        action_parameters_sha256=intent.parameters_sha256,
        policy_version="refund-v1",
        requested_by="agent-service",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        run_id=wait.run_id,
        wait_id=wait.wait_id,
        wait_generation=wait.generation,
    )


def test_decision_is_bound_and_idempotent(db: Database) -> None:
    intent = _seed(db)
    repository = ApprovalRepository()
    request = _request(intent)
    with db.transaction() as conn:
        first, replayed = repository.request(conn, request)
        assert first.decision == "PENDING" and not replayed
        assert conn.execute(
            "SELECT event_type FROM aftercare_outbox WHERE tenant_id=%s AND case_id=%s "
            "ORDER BY case_seq",
            (intent.tenant_id, intent.case_id),
        ).fetchall() == [("approval.requested",)]
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
        assert conn.execute(
            "SELECT event_type FROM aftercare_outbox WHERE tenant_id=%s AND case_id=%s "
            "ORDER BY case_seq",
            (intent.tenant_id, intent.case_id),
        ).fetchall() == [("approval.requested",), ("approval.decided",)]
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
        assert conn.execute(
            "SELECT count(*) FROM aftercare_outbox WHERE tenant_id=%s AND case_id=%s",
            (intent.tenant_id, intent.case_id),
        ).fetchone() == (2,)
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
    request = _request(intent, expires=timedelta(seconds=1))
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
        time.sleep(1.2)
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


def test_bound_approval_decision_wakes_waiting_run_atomically(db: Database) -> None:
    intent, wait = _seed_bound(db, "approval-wakeup")
    repository = ApprovalRepository()
    request = _bound_request(intent, wait)
    with db.transaction() as conn:
        repository.request(conn, request)
        decision = repository.decide(
            conn,
            intent.tenant_id,
            request.approval_id,
            approver="ops-reviewer",
            decision="APPROVED",
            decision_idempotency_key="decision-wakeup",
        )
        assert decision.decision == "APPROVED"
        run_row = conn.execute(
            "SELECT state,wait_id,wait_generation FROM aftercare_runs "
            "WHERE tenant_id=%s AND run_id=%s",
            (intent.tenant_id, wait.run_id),
        ).fetchone()
        wait_row = conn.execute(
            "SELECT state,resolved_by_event_id FROM aftercare_waits "
            "WHERE tenant_id=%s AND wait_id=%s",
            (intent.tenant_id, wait.wait_id),
        ).fetchone()
        inbox_row = conn.execute(
            "SELECT source_id,kind,correlation_key FROM aftercare_inbox "
            "WHERE tenant_id=%s AND source_event_id=%s",
            (intent.tenant_id, "approval-1:decision-wakeup"),
        ).fetchone()
        assert run_row == ("READY", None, None)
        assert wait_row is not None and wait_row[0] == "SATISFIED"
        assert wait_row[1] == "approval:approval-1:decision-wakeup"
        assert inbox_row == ("approval-service", "approval", "approval-1")
        replay, replayed = repository.request(conn, request)
        assert replay == decision and replayed
        assert (
            repository.decide(
                conn,
                intent.tenant_id,
                request.approval_id,
                approver="ops-reviewer",
                decision="APPROVED",
                decision_idempotency_key="decision-wakeup",
            )
            == decision
        )
        assert conn.execute(
            "SELECT count(*) FROM aftercare_wait_wakeups WHERE tenant_id=%s",
            (intent.tenant_id,),
        ).fetchone() == (1,)


def test_bound_approval_cannot_resurrect_timed_out_wait(db: Database) -> None:
    intent, wait = _seed_bound(db, "approval-timeout")
    repository = ApprovalRepository()
    request = _bound_request(intent, wait)
    with db.transaction() as conn:
        repository.request(conn, request)
        conn.execute(
            "UPDATE aftercare_waits SET deadline=clock_timestamp() - interval '1 second' "
            "WHERE tenant_id=%s AND wait_id=%s",
            (intent.tenant_id, wait.wait_id),
        )
        WaitRepository().resolve_timeout(conn, wait)
    with db.transaction() as conn:
        with pytest.raises(ContractViolation) as error:
            repository.decide(
                conn,
                intent.tenant_id,
                request.approval_id,
                approver="ops-reviewer",
                decision="APPROVED",
                decision_idempotency_key="decision-late",
            )
        assert error.value.code is ErrorCode.CONFLICT


def test_rejected_approval_wakes_but_action_remains_fail_closed(db: Database) -> None:
    intent, wait = _seed_bound(db, "approval-rejected")
    repository = ApprovalRepository()
    request = _bound_request(intent, wait)
    with db.transaction() as conn:
        repository.request(conn, request)
        decision = repository.decide(
            conn,
            intent.tenant_id,
            request.approval_id,
            approver="ops-reviewer",
            decision="REJECTED",
            decision_idempotency_key="decision-rejected",
            decision_reason="insufficient evidence",
        )
        assert decision.decision == "REJECTED"
        claim = RunRepository().claim(
            conn, intent.tenant_id, wait.run_id, "worker-after-reject", timedelta(seconds=30)
        )
        with pytest.raises(ContractViolation) as error:
            ActionRepository().mark_requested(
                conn,
                intent.action_id,
                claim,
                approval_id=request.approval_id,
                policy_version=request.policy_version,
            )
        assert error.value.code is ErrorCode.FORBIDDEN
        stored = ActionRepository().get(conn, intent.tenant_id, intent.action_id)
        assert stored is not None and stored.state == "RESERVED"


def test_decision_and_wait_wakeup_roll_back_together(db: Database) -> None:
    intent, wait = _seed_bound(db, "approval-rollback")
    repository = ApprovalRepository()
    request = _bound_request(intent, wait)
    with db.transaction() as conn:
        repository.request(conn, request)
    with pytest.raises(RuntimeError, match="abort-host-transaction"):
        with db.transaction() as conn:
            repository.decide(
                conn,
                intent.tenant_id,
                request.approval_id,
                approver="ops-reviewer",
                decision="APPROVED",
                decision_idempotency_key="decision-rollback",
            )
            raise RuntimeError("abort-host-transaction")
    with db.transaction() as conn:
        approval = repository.get(conn, intent.tenant_id, request.approval_id)
        assert approval is not None and approval.decision == "PENDING"
        assert conn.execute(
            "SELECT state FROM aftercare_runs WHERE tenant_id=%s AND run_id=%s",
            (intent.tenant_id, wait.run_id),
        ).fetchone() == ("WAITING_APPROVAL",)
        assert conn.execute(
            "SELECT count(*) FROM aftercare_inbox WHERE tenant_id=%s",
            (intent.tenant_id,),
        ).fetchone() == (0,)
