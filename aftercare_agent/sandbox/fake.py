"""A deterministic sandbox provider used to test lifecycle invariants.

This is not a security boundary.  It models the control-plane contract that a
real E2B/Firecracker/Kubernetes provider must satisfy: idempotent allocation,
fenced leases, bounded artifacts, and explicit destroy confirmation before a
capacity slot is released.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import RLock
from typing import Literal

from pydantic import Field

from aftercare_agent.domain.common import ContractModel, ContractViolation, ErrorCode, Identifier

type SandboxState = Literal["READY", "RUNNING", "DESTROY_REQUESTED", "DESTROYED"]


class SandboxSpec(ContractModel):
    image: Identifier
    cpu_millis: int = Field(ge=1, le=1_000_000)
    memory_mib: int = Field(ge=1, le=1_048_576)
    max_artifact_bytes: int = Field(default=1_000_000, ge=1, le=100_000_000)


class SandboxAllocation(ContractModel):
    allocation_id: Identifier
    request_key: Identifier
    owner: Identifier
    fencing_token: int
    state: SandboxState
    lease_until: datetime
    spec: SandboxSpec


class ArtifactRecord(ContractModel):
    allocation_id: Identifier
    name: Identifier
    sha256: str
    size_bytes: int


@dataclass
class _MutableAllocation:
    allocation_id: str
    request_key: str
    owner: str
    fencing_token: int
    state: SandboxState
    lease_until: datetime
    spec: SandboxSpec


class FakeSandboxProvider:
    """Thread-safe fake control plane; all methods are bounded and idempotent."""

    def __init__(self, *, capacity: int) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "sandbox capacity must be positive")
        self.capacity = capacity
        self._allocations: dict[str, _MutableAllocation] = {}
        self._requests: dict[str, str] = {}
        self._artifacts: dict[tuple[str, str], ArtifactRecord] = {}
        self._lock = RLock()

    def _public(self, value: _MutableAllocation) -> SandboxAllocation:
        return SandboxAllocation(
            allocation_id=value.allocation_id,
            request_key=value.request_key,
            owner=value.owner,
            fencing_token=value.fencing_token,
            state=value.state,
            lease_until=value.lease_until,
            spec=value.spec,
        )

    def create(
        self, *, request_key: str, owner: str, spec: SandboxSpec, lease: timedelta
    ) -> SandboxAllocation:
        if not request_key or not owner or lease <= timedelta(0):
            raise ContractViolation(ErrorCode.INVALID_INPUT, "invalid sandbox allocation request")
        with self._lock:
            existing_id = self._requests.get(request_key)
            if existing_id is not None:
                existing = self._allocations[existing_id]
                if existing.spec != spec:
                    raise ContractViolation(ErrorCode.CONFLICT, "sandbox request key reused")
                return self._public(existing)
            active = sum(item.state != "DESTROYED" for item in self._allocations.values())
            if active >= self.capacity:
                raise ContractViolation(ErrorCode.RATE_LIMITED, "sandbox capacity exhausted")
            allocation_id = f"sandbox-{len(self._allocations) + 1}"
            item = _MutableAllocation(
                allocation_id,
                request_key,
                owner,
                1,
                "READY",
                datetime.now(UTC) + lease,
                spec,
            )
            self._allocations[allocation_id] = item
            self._requests[request_key] = allocation_id
            return self._public(item)

    def renew(
        self, allocation_id: str, *, owner: str, fencing_token: int, lease: timedelta
    ) -> SandboxAllocation:
        with self._lock:
            item = self._require_current(allocation_id, owner, fencing_token)
            if item.state in ("DESTROY_REQUESTED", "DESTROYED"):
                raise ContractViolation(ErrorCode.CONFLICT, "sandbox is not renewable")
            item.lease_until = datetime.now(UTC) + lease
            return self._public(item)

    def request_destroy(
        self, allocation_id: str, *, owner: str, fencing_token: int
    ) -> SandboxAllocation:
        with self._lock:
            item = self._require_current(allocation_id, owner, fencing_token)
            if item.state == "DESTROYED":
                return self._public(item)
            item.state = "DESTROY_REQUESTED"
            return self._public(item)

    def confirm_destroy(self, allocation_id: str) -> SandboxAllocation:
        with self._lock:
            item = self._allocations.get(allocation_id)
            if item is None:
                raise ContractViolation(ErrorCode.INVALID_INPUT, "unknown sandbox allocation")
            item.state = "DESTROYED"
            return self._public(item)

    def put_artifact(
        self,
        allocation_id: str,
        *,
        owner: str,
        fencing_token: int,
        name: str,
        content: bytes,
    ) -> ArtifactRecord:
        with self._lock:
            item = self._require_current(allocation_id, owner, fencing_token)
            if item.state in ("DESTROY_REQUESTED", "DESTROYED"):
                raise ContractViolation(ErrorCode.CONFLICT, "sandbox is not writable")
            if len(content) > item.spec.max_artifact_bytes:
                raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "artifact size budget exceeded")
            if not name:
                raise ContractViolation(ErrorCode.INVALID_INPUT, "artifact name is required")
            record = ArtifactRecord(
                allocation_id=allocation_id,
                name=name,
                sha256=sha256(content).hexdigest(),
                size_bytes=len(content),
            )
            self._artifacts[(allocation_id, name)] = record
            return record

    def reconcile(self, *, now: datetime) -> tuple[SandboxAllocation, ...]:
        """Mark expired live allocations for destroy; capacity remains reserved."""
        with self._lock:
            for item in self._allocations.values():
                if item.state in ("READY", "RUNNING") and item.lease_until <= now:
                    item.state = "DESTROY_REQUESTED"
            return tuple(self._public(item) for item in self._allocations.values())

    def _require_current(
        self, allocation_id: str, owner: str, fencing_token: int
    ) -> _MutableAllocation:
        item = self._allocations.get(allocation_id)
        if item is None or item.owner != owner or item.fencing_token != fencing_token:
            raise ContractViolation(ErrorCode.LEASE_LOST, "sandbox lease lost")
        if item.lease_until <= datetime.now(UTC) and item.state != "DESTROYED":
            raise ContractViolation(ErrorCode.LEASE_LOST, "sandbox lease expired")
        return item
