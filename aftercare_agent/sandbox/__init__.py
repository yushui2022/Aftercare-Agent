"""Bounded sandbox lifecycle contracts and a deterministic local provider."""

from .fake import (
    ArtifactRecord,
    FakeSandboxProvider,
    SandboxAllocation,
    SandboxSpec,
)

__all__ = ["ArtifactRecord", "FakeSandboxProvider", "SandboxAllocation", "SandboxSpec"]
