"""Allocation policy above a provider: workload tiers, leases and reclamation.

A provider owns the question "is this resource still mine".  The manager owns
"which workload class gets which shape, under which request key, and when a
finished step gives its capacity back".  Keeping tiers here instead of inside
the provider is what lets one deployment run a cheap lookup and a heavier
document step in differently shaped sandboxes without teaching the provider
anything about business classes.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Self

from pydantic import model_validator

from aftercare_agent.domain.common import (
    ContractModel,
    ContractViolation,
    ErrorCode,
    Identifier,
)

from .contracts import SandboxProvider
from .models import ArtifactRecord, SandboxAllocation, SandboxSpec


class SandboxTierPolicy(ContractModel):
    """Deployment-owned mapping from a workload class to a sandbox shape."""

    tiers: Mapping[Identifier, SandboxSpec]

    @model_validator(mode="after")
    def at_least_one_tier(self) -> Self:
        if not self.tiers:
            raise ValueError("sandbox tier policy needs at least one tier")
        return self

    def spec_for(self, tier: str) -> SandboxSpec:
        spec = self.tiers.get(tier)
        if spec is None:
            # Never fall back to another tier: a sandbox larger than the class
            # asked for is a silently different isolation and cost profile.
            raise ContractViolation(ErrorCode.CONFLICT, "unknown sandbox tier")
        return spec


class SandboxSweep(ContractModel):
    """Counts from one reclamation pass; capacity still leaves only on confirmation."""

    active: int
    pending_destroy: int
    destroyed: int


class SandboxManager:
    """Tiered allocation, renewal and two-phase release over one provider."""

    def __init__(
        self, provider: SandboxProvider, policy: SandboxTierPolicy, *, lease: timedelta
    ) -> None:
        if lease <= timedelta(0):
            raise ContractViolation(ErrorCode.INVALID_INPUT, "sandbox lease must be positive")
        self._provider = provider
        self._policy = policy
        self._lease = lease

    @property
    def lease(self) -> timedelta:
        return self._lease

    def acquire(self, *, request_key: str, tier: str, owner: str) -> SandboxAllocation:
        """Allocate for one workload class; replaying the same key returns the original.

        Recovery after a lost create response is the provider's idempotency, so
        the manager deliberately keeps no second allocation table that could
        disagree with the resource the provider actually fenced.
        """
        return self._provider.create(
            request_key=request_key,
            owner=owner,
            spec=self._policy.spec_for(tier),
            lease=self._lease,
        )

    def begin(self, allocation: SandboxAllocation, *, owner: str) -> SandboxAllocation:
        """Move a leased allocation into RUNNING; repeating this is idempotent."""
        return self._provider.start(
            allocation.allocation_id, owner=owner, fencing_token=allocation.fencing_token
        )

    def renew(self, allocation: SandboxAllocation, *, owner: str) -> SandboxAllocation:
        return self._provider.renew(
            allocation.allocation_id,
            owner=owner,
            fencing_token=allocation.fencing_token,
            lease=self._lease,
        )

    def publish(
        self,
        allocation: SandboxAllocation,
        *,
        owner: str,
        name: str,
        content: bytes,
    ) -> ArtifactRecord:
        return self._provider.put_artifact(
            allocation.allocation_id,
            owner=owner,
            fencing_token=allocation.fencing_token,
            name=name,
            content=content,
        )

    def request_release(self, allocation: SandboxAllocation, *, owner: str) -> SandboxAllocation:
        requested = self._provider.request_destroy(
            allocation.allocation_id, owner=owner, fencing_token=allocation.fencing_token
        )
        if requested.state not in ("DESTROY_REQUESTED", "DESTROYED"):
            raise ContractViolation(
                ErrorCode.CONFLICT, "provider did not accept the destroy request"
            )
        return requested

    def confirm_release(self, allocation: SandboxAllocation) -> SandboxAllocation:
        """Accept the provider's own confirmation; never inferred from a timeout."""
        confirmed = self._provider.confirm_destroy(allocation.allocation_id)
        if confirmed.state != "DESTROYED":
            raise ContractViolation(ErrorCode.CONFLICT, "provider has not confirmed the destroy")
        return confirmed

    def release(self, allocation: SandboxAllocation, *, owner: str) -> SandboxAllocation:
        """Request and accept confirmation in one call, for a step that finished."""
        return self.confirm_release(self.request_release(allocation, owner=owner))

    def sweep(self, *, now: datetime) -> SandboxSweep:
        """Reconcile expired leases; confirmation stays an explicit provider answer."""
        allocations = self._provider.reconcile(now=now)
        return SandboxSweep(
            active=sum(a.state in ("READY", "RUNNING") for a in allocations),
            pending_destroy=sum(a.state == "DESTROY_REQUESTED" for a in allocations),
            destroyed=sum(a.state == "DESTROYED" for a in allocations),
        )
