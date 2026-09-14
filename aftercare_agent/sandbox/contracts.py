"""Provider-neutral contracts for sandbox lifecycle control.

The application owns the run lease and business authorization.  A sandbox
provider only owns the short-lived execution environment and must enforce its
own allocation fence.  Keeping this protocol separate from the fake provider
lets a Kubernetes or E2B adapter be tested without importing either SDK into
the trusted Aftercare service.
"""

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from .models import ArtifactRecord, SandboxAllocation, SandboxSpec


@runtime_checkable
class SandboxProvider(Protocol):
    """Minimal control-plane surface required by a sandbox manager.

    ``create`` is idempotent by ``request_key``.  ``start`` is an explicit
    READY -> RUNNING transition; providers must not infer that a newly
    allocated warm sandbox is already executing work.  Every mutating method
    after creation is fenced by ``owner`` and ``fencing_token``.
    """

    def create(
        self, *, request_key: str, owner: str, spec: SandboxSpec, lease: timedelta
    ) -> SandboxAllocation: ...

    def start(self, allocation_id: str, *, owner: str, fencing_token: int) -> SandboxAllocation: ...

    def renew(
        self,
        allocation_id: str,
        *,
        owner: str,
        fencing_token: int,
        lease: timedelta,
    ) -> SandboxAllocation: ...

    def request_destroy(
        self, allocation_id: str, *, owner: str, fencing_token: int
    ) -> SandboxAllocation: ...

    def confirm_destroy(self, allocation_id: str) -> SandboxAllocation: ...

    def put_artifact(
        self,
        allocation_id: str,
        *,
        owner: str,
        fencing_token: int,
        name: str,
        content: bytes,
    ) -> ArtifactRecord: ...

    def reconcile(self, *, now: datetime) -> tuple[SandboxAllocation, ...]: ...
