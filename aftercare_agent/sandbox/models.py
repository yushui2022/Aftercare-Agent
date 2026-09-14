"""Validated value objects shared by sandbox providers and their adapters."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from aftercare_agent.domain.common import ContractModel, Identifier

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
