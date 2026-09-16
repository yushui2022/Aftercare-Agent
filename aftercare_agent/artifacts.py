"""Content-addressed storage for authorized evidence bytes.

A checkpoint only records an :class:`ArtifactReference` -- tenant, case, a
local reference id and a SHA-256 -- so reading evidence back needs a store
that re-checks scope and digest instead of trusting the pointer.  This module
is that store: one file per digest under a deployment-owned root, written
atomically, never rewritten with different bytes, bounded by an explicit size
budget, and holding no database connection or credential of its own.
"""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

from aftercare_agent.domain.common import (
    CaseScope,
    ContractViolation,
    ErrorCode,
    require_same_case,
)
from aftercare_agent.domain.protocol import ArtifactReference

MAX_ARTIFACT_BYTES = 100_000_000
_SEPARATORS = ("/", "\\", "\x00")


def _segment(value: str, label: str) -> str:
    """Identifiers may contain ``/``, so a scope id is not path-safe on its own."""
    if value in {".", ".."} or any(separator in value for separator in _SEPARATORS):
        raise ContractViolation(ErrorCode.INVALID_INPUT, f"{label} is not a single path segment")
    return value


class ContentAddressedArtifactStore:
    """Immutable bytes addressed by SHA-256, partitioned by tenant and case."""

    def __init__(self, root: str | Path, *, max_bytes: int = 1_048_576) -> None:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ARTIFACT_BYTES:
            raise ContractViolation(
                ErrorCode.INVALID_INPUT, f"max_bytes must be between 1 and {MAX_ARTIFACT_BYTES}"
            )
        self._root = Path(root).resolve()
        self._max_bytes = max_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def _path(self, scope: CaseScope, digest: str) -> Path:
        path = (
            self._root
            / _segment(scope.tenant_id, "tenant_id")
            / _segment(scope.case_id, "case_id")
            / digest
        )
        # Defence in depth: a later change to the identifier rules must not be
        # able to write outside the configured root.
        if path.parent.parent.parent != self._root:
            raise ContractViolation(ErrorCode.FORBIDDEN, "artifact path escapes the store root")
        return path

    def put(self, scope: CaseScope, *, reference_id: str, content: bytes) -> ArtifactReference:
        if not isinstance(content, bytes):
            raise ContractViolation(ErrorCode.INVALID_INPUT, "artifact content must be bytes")
        if not content:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "artifact content must not be empty")
        if len(content) > self._max_bytes:
            raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "artifact exceeds the store budget")
        digest = sha256(content).hexdigest()
        target = self._path(scope, digest)
        if target.exists():
            # The same digest means the same bytes, so a replay is a no-op.  A
            # file that no longer matches its own name is corruption, not
            # something to overwrite silently.
            if sha256(target.read_bytes()).hexdigest() != digest:
                raise ContractViolation(
                    ErrorCode.CONFLICT, "stored artifact does not match its digest"
                )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            pending = target.with_name(f".{digest}.{os.getpid()}.pending")
            pending.write_bytes(content)
            os.replace(pending, target)
        return ArtifactReference(
            tenant_id=scope.tenant_id,
            case_id=scope.case_id,
            reference_id=reference_id,
            sha256=digest,
        )

    def read(self, scope: CaseScope, reference: ArtifactReference) -> bytes:
        require_same_case(scope, reference)
        target = self._path(reference, reference.sha256)
        try:
            content = target.read_bytes()
        except FileNotFoundError as exc:
            raise ContractViolation(ErrorCode.CONFLICT, "artifact content is missing") from exc
        if sha256(content).hexdigest() != reference.sha256:
            raise ContractViolation(
                ErrorCode.CONFLICT, "artifact content does not match its digest"
            )
        return content

    def holds(self, scope: CaseScope, reference: ArtifactReference) -> bool:
        require_same_case(scope, reference)
        return self._path(reference, reference.sha256).is_file()
