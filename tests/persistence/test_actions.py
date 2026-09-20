"""PostgreSQL Action Ledger tests with a deterministic provider double."""

from datetime import UTC, datetime, timedelta

import pytest

from aftercare_agent.actions import ActionIntent, ProviderReceipt
from aftercare_agent.domain.approvals import ApprovalRequest
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import CaseRecord, RunRecord
from aftercare_agent.persistence import (
    ActionRepository,
    ApprovalRepository,
    Database,
    RunRepository,
)


def _intent(
    tenant: str,
    *,
    case_id: str = "case-1",
    action_id: str = "action-1",
    amount: str = "2500",
    business: str = "payment:p1:refund:r1",
    idem: str = "request-1",
    approval_required: bool = True,
) -> ActionIntent:
    return ActionIntent(
        tenant_id=tenant,
        case_id=case_id,
        order_id="order-1",
        action_id=action_id,
        action_type="refund",
        business_key=business,
        idempotency_key=idem,
        parameters_sha256="a" * 64,
        amount_minor=amount,
        currency="USD",
        provider_idempotency_key="refund:r1",
        approval_required=approval_required,
    )


def _seed(db: Database, tenant: str, case_id: str, run_id: str) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM aftercare_actions WHERE tenant_id=%s", (tenant,))
        conn.execute("DELETE FROM aftercare_runs WHERE tenant_id=%s", (tenant,))
        conn.execute("DELETE FROM aftercare_cases WHERE tenant_id=%s", (tenant,))
        runs = RunRepository()
        runs.create_case(
            conn,
            CaseRecord(tenant_id=tenant, case_id=case_id, order_id="order-1", version=1),
        )
        runs.create_run(
            conn,
            RunRecord(
                tenant_id=tenant,
                case_id=case_id,
                run_id=run_id,
                definition_version="v1",
                input_version=1,
            ),
        )


def _approve(db: Database, tenant: str, action_id: str, *, policy: str = "refund-v1") -> str:
    with db.transaction() as conn:
        action = ActionRepository().get(conn, tenant, action_id)
        assert action is not None
        request = ApprovalRequest(
            tenant_id=tenant,
            case_id=action.case_id,
            approval_id=f"approval-{action_id}",
            action_id=action.action_id,
            action_parameters_sha256=action.parameters_sha256,
            policy_version=policy,
            requested_by="agent-service",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        ApprovalRepository().request(conn, request)
        ApprovalRepository().decide(
            conn,
            tenant,
            request.approval_id,
            approver="ops-reviewer",
            decision="APPROVED",
            decision_idempotency_key=f"decision-{action_id}",
        )
    return request.approval_id


def test_reserved_action_fails_closed_until_approved(db: Database) -> None:
    tenant, case_id, run_id = "action-gate", "case-1", "run-1"
    _seed(db, tenant, case_id, run_id)
    ledger = ActionRepository()
    runs = RunRepository()
    with db.transaction() as conn:
        action = ledger.reserve(conn, _intent(tenant)).action
        claim = runs.claim(conn, tenant, run_id, "worker-a", timedelta(seconds=30))
        with pytest.raises(ContractViolation) as error:
            ledger.mark_requested(conn, action.action_id, claim, policy_version="refund-v1")
        assert error.value.code is ErrorCode.FORBIDDEN
    approval_id = _approve(db, tenant, "action-1")
    with db.transaction() as conn:
        requested = ledger.mark_requested(
            conn,
            "action-1",
            claim,
            approval_id=approval_id,
            policy_version="refund-v1",
        )
        assert requested.state == "REQUESTED"


def test_explicitly_exempt_internal_action_can_dispatch_without_approval(db: Database) -> None:
    tenant, case_id, run_id = "action-exempt", "case-1", "run-1"
    _seed(db, tenant, case_id, run_id)
    ledger = ActionRepository()
    runs = RunRepository()
    intent = _intent(tenant, approval_required=False)
    with db.transaction() as conn:
        action = ledger.reserve(conn, intent).action
        claim = runs.claim(conn, tenant, run_id, "worker-a", timedelta(seconds=30))
        requested = ledger.mark_requested(conn, action.action_id, claim)
        assert requested.state == "REQUESTED"


class SyntheticProvider:
    """A test-only provider: no network and no production side effects."""

    def request(self, action: object) -> tuple[str, str]:
        del action
        return "synthetic-receipt-1", "b" * 64


def test_reservation_replays_and_deduplicates_cross_case_obligation(db: Database) -> None:
    tenant = "action-replay"
    _seed(db, tenant, "case-1", "run-1")
    with db.transaction() as conn:
        ledger = ActionRepository()
        first = ledger.reserve(conn, _intent(tenant))
        replay = ledger.reserve(conn, _intent(tenant))
        assert not first.replayed and replay.replayed
        assert replay.action.action_id == "action-1"
        # A second Case can point to the same business obligation, but does not
        # create another provider operation.
        RunRepository().create_case(
            conn,
            CaseRecord(tenant_id=tenant, case_id="case-2", order_id="order-1", version=1),
        )
        cross = ledger.reserve(
            conn, _intent(tenant, case_id="case-2", action_id="action-2", idem="request-2")
        )
        assert cross.replayed and cross.action.case_id == "case-1"


def test_changed_amount_or_idempotency_input_is_a_conflict(db: Database) -> None:
    tenant = "action-conflict"
    _seed(db, tenant, "case-1", "run-1")
    with db.transaction() as conn:
        ledger = ActionRepository()
        ledger.reserve(conn, _intent(tenant))
        with pytest.raises(ContractViolation) as amount:
            ledger.reserve(conn, _intent(tenant, amount="2501"))
        assert amount.value.code is ErrorCode.CONFLICT
        with pytest.raises(ContractViolation) as idem:
            ledger.reserve(
                conn, _intent(tenant, action_id="action-2", business="payment:p1:refund:r2")
            )
        assert idem.value.code is ErrorCode.CONFLICT


def test_result_requires_current_claim_and_unknown_is_not_retried(db: Database) -> None:
    tenant, case_id, run_id = "action-fence", "case-1", "run-1"
    _seed(db, tenant, case_id, run_id)
    ledger = ActionRepository()
    runs = RunRepository()
    with db.transaction() as conn:
        action = ledger.reserve(conn, _intent(tenant)).action
    approval_id = _approve(db, tenant, action.action_id)
    with db.transaction() as conn:
        claim = runs.claim(conn, tenant, run_id, "worker-a", timedelta(seconds=30))
        ledger.mark_requested(
            conn,
            action.action_id,
            claim,
            approval_id=approval_id,
            policy_version="refund-v1",
        )
        provider = SyntheticProvider()
        receipt, digest = provider.request(action)
        unknown = ledger.mark_receipt(
            conn,
            ProviderReceipt(
                action_id=action.action_id,
                provider_idempotency_key=action.provider_idempotency_key,
                state="UNKNOWN",
            ),
            claim,
        )
        assert unknown.state == "UNKNOWN"
        assert unknown.amount_minor == "2500"
        # An UNKNOWN result has no blind retry transition.  Reconciliation can
        # use the same claim to confirm the original operation.
        confirmed = ledger.mark_receipt(
            conn,
            ProviderReceipt(
                action_id=action.action_id,
                provider_idempotency_key=action.provider_idempotency_key,
                state="CONFIRMED",
                provider_reference=receipt,
                result_sha256=digest,
            ),
            claim,
        )
        assert confirmed.state == "CONFIRMED"
        assert confirmed.fencing_token == claim.fencing_token
    with db.transaction() as conn:
        with pytest.raises(ContractViolation) as error:
            ledger.mark_result(
                conn,
                "action-1",
                claim,
                state="FAILED",
                failure_code="late-worker",
            )
        assert error.value.code is ErrorCode.CONFLICT


def test_stale_claim_cannot_record_provider_result(db: Database) -> None:
    tenant, case_id, run_id = "action-stale", "case-1", "run-1"
    _seed(db, tenant, case_id, run_id)
    ledger = ActionRepository()
    approval_id: str
    with db.transaction() as conn:
        action = ledger.reserve(conn, _intent(tenant)).action
    approval_id = _approve(db, tenant, action.action_id)
    with db.transaction() as conn:
        first = RunRepository().claim(conn, tenant, run_id, "worker-a", timedelta(seconds=30))
        ledger.mark_requested(
            conn,
            action.action_id,
            first,
            approval_id=approval_id,
            policy_version="refund-v1",
        )
        conn.execute(
            "UPDATE aftercare_runs SET lease_until=clock_timestamp() - interval '1 second' "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, run_id),
        )
        second = RunRepository().claim(conn, tenant, run_id, "worker-b", timedelta(seconds=30))
        with pytest.raises(ContractViolation) as error:
            ledger.mark_result(
                conn,
                action.action_id,
                first,
                state="UNKNOWN",
            )
        assert error.value.code is ErrorCode.LEASE_LOST
        result = ledger.mark_result(conn, action.action_id, second, state="UNKNOWN")
        assert result.state == "UNKNOWN"
