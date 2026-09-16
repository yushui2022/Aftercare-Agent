"""Bounded sandbox lifecycle contracts, tier policy and a deterministic local provider."""

from .contracts import SandboxProvider
from .fake import FakeSandboxProvider
from .manager import SandboxManager, SandboxSweep, SandboxTierPolicy
from .models import ArtifactRecord, SandboxAllocation, SandboxSpec, SandboxState

__all__ = [
    "ArtifactRecord",
    "FakeSandboxProvider",
    "SandboxAllocation",
    "SandboxManager",
    "SandboxProvider",
    "SandboxSpec",
    "SandboxState",
    "SandboxSweep",
    "SandboxTierPolicy",
]
