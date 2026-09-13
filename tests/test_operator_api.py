import pytest
from pydantic import ValidationError

from aftercare_agent.api.app import ApprovalDecisionInput, ReviewDecisionInput
from aftercare_agent.auth import synthetic_context
from aftercare_agent.domain.common import ContractViolation, ErrorCode


def test_operator_decision_inputs_forbid_authority_fields() -> None:
    with pytest.raises(ValidationError):
        ReviewDecisionInput.model_validate({"decision": "CONTINUE", "reviewer": "attacker"})
    with pytest.raises(ValidationError):
        ApprovalDecisionInput.model_validate(
            {"decision": "APPROVED", "approver": "attacker", "tenant_id": "other"}
        )


def test_operator_decision_inputs_are_strict_and_bounded() -> None:
    with pytest.raises(ValidationError):
        ReviewDecisionInput.model_validate({"decision": "continue"})
    with pytest.raises(ValidationError):
        ApprovalDecisionInput.model_validate({"decision": "APPROVED", "decision_reason": 1})
    with pytest.raises(ValidationError):
        ReviewDecisionInput.model_validate({"decision": "CANCEL", "decision_reason": "x" * 2001})


def test_synthetic_operator_scope_is_explicit_and_not_request_controlled() -> None:
    context = synthetic_context(enabled=True, tenant_id="tenant-1", subject_id="operator-1")
    assert context.subject_id == "operator-1"
    assert "review:decide" in context.permissions
    assert "approval:decide" in context.permissions
    with pytest.raises(ContractViolation) as missing:
        synthetic_context(enabled=False, tenant_id="tenant-1", subject_id="operator-1")
    assert missing.value.code is ErrorCode.UNAUTHENTICATED
