from datetime import UTC, datetime, timedelta

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.sandbox import (
    FakeSandboxProvider,
    SandboxManager,
    SandboxSpec,
    SandboxTierPolicy,
)

POLICY = SandboxTierPolicy(
    tiers={
        "lookup": SandboxSpec(image="tools", cpu_millis=250, memory_mib=256),
        "draft": SandboxSpec(image="tools", cpu_millis=1000, memory_mib=1024),
    }
)
LEASE = timedelta(minutes=5)


def _manager(capacity: int = 1) -> SandboxManager:
    return SandboxManager(FakeSandboxProvider(capacity=capacity), POLICY, lease=LEASE)


def test_tier_selects_its_own_shape() -> None:
    manager = _manager()
    lookup = manager.acquire(request_key="k1", tier="lookup", owner="worker-a")
    assert lookup.spec == POLICY.tiers["lookup"]
    assert lookup.spec != POLICY.tiers["draft"]


def test_unknown_tier_fails_closed_instead_of_falling_back() -> None:
    manager = _manager()
    with pytest.raises(ContractViolation) as error:
        manager.acquire(request_key="k1", tier="gpu", owner="worker-a")
    assert error.value.code is ErrorCode.CONFLICT


def test_acquire_replay_returns_the_same_allocation() -> None:
    manager = _manager(capacity=2)
    first = manager.acquire(request_key="k1", tier="lookup", owner="worker-a")
    assert manager.acquire(request_key="k1", tier="lookup", owner="worker-a") == first


def test_capacity_returns_only_after_a_confirmed_destroy() -> None:
    manager = _manager(capacity=1)
    first = manager.acquire(request_key="k1", tier="lookup", owner="worker-a")
    requested = manager.request_release(first, owner="worker-a")
    assert requested.state == "DESTROY_REQUESTED"
    with pytest.raises(ContractViolation) as error:
        manager.acquire(request_key="k2", tier="lookup", owner="worker-b")
    assert error.value.code is ErrorCode.RATE_LIMITED
    assert manager.confirm_release(first).state == "DESTROYED"
    assert manager.acquire(request_key="k2", tier="lookup", owner="worker-b").state == "READY"


def test_release_completes_both_phases_and_repeats_safely() -> None:
    manager = _manager(capacity=2)
    allocation = manager.acquire(request_key="k1", tier="draft", owner="worker-a")
    assert manager.release(allocation, owner="worker-a").state == "DESTROYED"
    assert manager.release(allocation, owner="worker-a").state == "DESTROYED"


def test_a_stale_owner_cannot_release_by_fence() -> None:
    manager = _manager(capacity=2)
    allocation = manager.acquire(request_key="k1", tier="lookup", owner="worker-a")
    with pytest.raises(ContractViolation) as error:
        manager.release(allocation, owner="worker-b")
    assert error.value.code is ErrorCode.LEASE_LOST


def test_sweep_marks_expiry_but_keeps_the_capacity_reserved() -> None:
    manager = _manager(capacity=1)
    manager.acquire(request_key="k1", tier="lookup", owner="worker-a")
    sweep = manager.sweep(now=datetime.now(UTC) + LEASE + timedelta(seconds=1))
    assert (sweep.active, sweep.pending_destroy, sweep.destroyed) == (0, 1, 0)
    with pytest.raises(ContractViolation) as error:
        manager.acquire(request_key="k2", tier="lookup", owner="worker-b")
    assert error.value.code is ErrorCode.RATE_LIMITED


def test_publish_reports_the_digest_of_what_the_sandbox_received() -> None:
    manager = _manager()
    allocation = manager.acquire(request_key="k1", tier="lookup", owner="worker-a")
    record = manager.publish(
        allocation, owner="worker-a", name="call-1.json", content=b'{"ok":true}'
    )
    assert record.size_bytes == len(b'{"ok":true}')
    assert record == manager.publish(
        allocation, owner="worker-a", name="call-1.json", content=b'{"ok":true}'
    )
