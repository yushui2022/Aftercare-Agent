from datetime import UTC, datetime

import pytest

from aftercare_agent.actions import (
    ActionRecord,
    ProviderReceipt,
    SyntheticActionProvider,
    validate_provider_receipt,
)
from aftercare_agent.domain.common import ContractViolation, ErrorCode

NOW = datetime(2026, 9, 20, tzinfo=UTC)


def _action() -> ActionRecord:
    return ActionRecord(
        tenant_id="tenant-a",
        case_id="case-1",
        schema_version=1,
        order_id="order-1",
        action_id="action-1",
        action_type="refund",
        business_key="payment:p-1:refund:r-1",
        idempotency_key="request-1",
        parameters_sha256="a" * 64,
        amount_minor="100",
        currency="USD",
        provider_idempotency_key="refund:r-1",
        state="REQUESTED",
        created_at=NOW,
        updated_at=NOW,
    )


def test_provider_receipt_requires_evidence_for_terminal_outcomes() -> None:
    action = _action()
    receipt = ProviderReceipt(
        action_id=action.action_id,
        provider_idempotency_key=action.provider_idempotency_key,
        state="CONFIRMED",
        provider_reference="provider-1",
        result_sha256="b" * 64,
    )
    validate_provider_receipt(action, receipt)

    with pytest.raises(ValueError, match="result digest"):
        ProviderReceipt(action_id="action-1", state="CONFIRMED", provider_reference="provider-1")
    with pytest.raises(ValueError, match="failure code"):
        ProviderReceipt(action_id="action-1", state="FAILED")


def test_provider_receipt_cannot_be_attached_to_another_action() -> None:
    action = _action()
    receipt = ProviderReceipt(
        action_id="action-other",
        provider_idempotency_key=action.provider_idempotency_key,
        state="UNKNOWN",
    )

    with pytest.raises(ContractViolation) as caught:
        validate_provider_receipt(action, receipt)
    assert caught.value.code is ErrorCode.FORBIDDEN

    same_action_wrong_key = receipt.model_copy(
        update={"action_id": action.action_id, "provider_idempotency_key": "other-key"}
    )
    with pytest.raises(ContractViolation) as caught:
        validate_provider_receipt(action, same_action_wrong_key)
    assert caught.value.code is ErrorCode.CONFLICT


def test_synthetic_provider_reconciles_unknown_with_the_same_key() -> None:
    action = _action()
    provider = SyntheticActionProvider(unknown_first=True)

    unknown = provider.request(action)
    assert unknown.state == "UNKNOWN"
    confirmed = provider.lookup(action)
    assert confirmed.state == "CONFIRMED"
    assert confirmed.provider_idempotency_key == action.provider_idempotency_key
    assert provider.requests[action.provider_idempotency_key or ""] == 1
