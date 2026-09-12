from datetime import UTC, datetime, timedelta

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.sandbox import FakeSandboxProvider, SandboxSpec

SPEC = SandboxSpec(image="aftercare-tools", cpu_millis=250, memory_mib=256)


def test_create_is_idempotent_and_capacity_is_shared() -> None:
    provider = FakeSandboxProvider(capacity=1)
    first = provider.create(
        request_key="case-1", owner="worker-a", spec=SPEC, lease=timedelta(minutes=1)
    )
    replay = provider.create(
        request_key="case-1", owner="worker-a", spec=SPEC, lease=timedelta(minutes=1)
    )
    assert replay == first
    with pytest.raises(ContractViolation) as error:
        provider.create(
            request_key="case-2", owner="worker-b", spec=SPEC, lease=timedelta(minutes=1)
        )
    assert error.value.code is ErrorCode.RATE_LIMITED


def test_expiry_requires_destroy_confirmation_before_capacity_reuse() -> None:
    provider = FakeSandboxProvider(capacity=1)
    allocation = provider.create(
        request_key="case-1", owner="worker-a", spec=SPEC, lease=timedelta(minutes=1)
    )
    expired = provider.reconcile(now=datetime.now(UTC) + timedelta(minutes=2))
    assert expired[0].state == "DESTROY_REQUESTED"
    with pytest.raises(ContractViolation) as error:
        provider.create(
            request_key="case-2", owner="worker-b", spec=SPEC, lease=timedelta(minutes=1)
        )
    assert error.value.code is ErrorCode.RATE_LIMITED
    provider.confirm_destroy(allocation.allocation_id)
    replacement = provider.create(
        request_key="case-2", owner="worker-b", spec=SPEC, lease=timedelta(minutes=1)
    )
    assert replacement.allocation_id != allocation.allocation_id


def test_fencing_and_artifact_budget_are_enforced() -> None:
    provider = FakeSandboxProvider(capacity=1)
    limited_spec = SPEC.model_copy(update={"max_artifact_bytes": 2})
    allocation = provider.create(
        request_key="case-1",
        owner="worker-a",
        spec=limited_spec,
        lease=timedelta(minutes=1),
    )
    artifact = provider.put_artifact(
        allocation.allocation_id,
        owner="worker-a",
        fencing_token=1,
        name="result.json",
        content=b"ok",
    )
    assert artifact.size_bytes == 2
    with pytest.raises(ContractViolation) as error:
        provider.put_artifact(
            allocation.allocation_id,
            owner="worker-a",
            fencing_token=2,
            name="bad",
            content=b"x",
        )
    assert error.value.code is ErrorCode.LEASE_LOST
    with pytest.raises(ContractViolation) as error:
        provider.put_artifact(
            allocation.allocation_id,
            owner="worker-a",
            fencing_token=1,
            name="too-big",
            content=b"toolarge",
        )
    assert error.value.code is ErrorCode.BUDGET_EXHAUSTED
