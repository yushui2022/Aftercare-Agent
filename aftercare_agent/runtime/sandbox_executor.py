"""Execute one already-validated read-only intent inside a leased sandbox.

The model proposes, the Harness validates, and this module is where a
validated intent actually runs.  It is the only place that knows both a
sandbox provider and a business connector, and it still refuses to become an
authority: tenant, case, run and order come from the trusted Run scope and the
injected case binding, never from the tool arguments; every answer is stored
content-addressed before its digest is handed back to the checkpoint; and the
allocation is given back on the failure path as well, so a broken connector
cannot quietly leak capacity.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from aftercare_agent.artifacts import ContentAddressedArtifactStore
from aftercare_agent.connectors.commerce import CommerceConnector
from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope
from aftercare_agent.domain.protocol import (
    ArtifactReference,
    BoundLookupArguments,
    MaterialDraftArguments,
    ToolRequest,
    strict_json_object,
)
from aftercare_agent.sandbox.manager import SandboxManager
from aftercare_agent.sandbox.models import SandboxAllocation

MATERIAL_DRAFT_SCHEMA = "aftercare.material.draft.v1"
MAX_KEY_ATTEMPTS = 4


@runtime_checkable
class CaseBinding(Protocol):
    """Trusted mapping from a Run to the one order it may investigate."""

    def order_id_for(self, scope: RunScope) -> str:
        """Return the order the host bound to this Run; never a model input."""


@dataclass(frozen=True)
class StaticCaseBinding:
    """A deployment-supplied binding, as used by a single-run demo or test."""

    order_id: str

    def order_id_for(self, scope: RunScope) -> str:
        del scope
        return self.order_id


def _revalidated(request: ToolRequest) -> BoundLookupArguments | MaterialDraftArguments:
    """Re-check the schema here too: a checkpoint is durable input, not proof."""
    model = (
        MaterialDraftArguments if request.name == "request_material_draft" else BoundLookupArguments
    )
    strict_json_object(request.arguments_json)
    try:
        return model.model_validate_json(request.arguments_json)
    except ValidationError as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "tool arguments violate schema") from exc


class SandboxedToolExecutor:
    """The ``ToolExecutor`` a model-driven Harness is given in production shape."""

    def __init__(
        self,
        *,
        manager: SandboxManager,
        connector: CommerceConnector,
        store: ContentAddressedArtifactStore,
        bindings: CaseBinding,
        tiers: Mapping[str, str],
        owner: str,
        max_result_bytes: int = 262_144,
    ) -> None:
        if not owner:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "sandbox owner is required")
        if type(max_result_bytes) is not int or max_result_bytes < 1:
            raise ContractViolation(ErrorCode.INVALID_INPUT, "max_result_bytes must be positive")
        self._manager = manager
        self._connector = connector
        self._store = store
        self._bindings = bindings
        self._tiers = dict(tiers)
        self._owner = owner
        self._max_result_bytes = max_result_bytes

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def store(self) -> ContentAddressedArtifactStore:
        """The evidence store this executor writes to; reads stay scope-checked."""
        return self._store

    def execute(self, request: ToolRequest, *, scope: RunScope, now: datetime) -> ArtifactReference:
        tier = self._tiers.get(request.name)
        if tier is None:
            raise ContractViolation(
                ErrorCode.FORBIDDEN, "no sandbox tier is configured for this tool"
            )
        order_id = self._bindings.order_id_for(scope)
        allocation = self._acquire(request=request, scope=scope, tier=tier)
        try:
            reference = self._run(request, scope=scope, allocation=allocation, order_id=order_id)
        except BaseException:
            self._release_after_failure(allocation)
            raise
        self._manager.release(allocation, owner=self._owner)
        return reference

    def _acquire(self, *, request: ToolRequest, scope: RunScope, tier: str) -> SandboxAllocation:
        """Allocate idempotently by key, and separately from a previous attempt.

        ``create`` is idempotent by ``request_key``, which is what makes a lost
        create response recoverable.  A key that resolves to an allocation this
        slice already destroyed means an earlier attempt finished its sandbox
        but never recorded the result, so the retry gets a fresh allocation
        under a fresh key: a destroyed sandbox cannot be restarted, and the
        evidence must not be attributed to a resource that no longer exists.
        """
        base = f"{scope.tenant_id}:{scope.case_id}:{scope.run_id}:{request.call_id}:{self._owner}"
        for attempt in range(1, MAX_KEY_ATTEMPTS + 1):
            key = base if attempt == 1 else f"{base}:retry-{attempt}"
            allocation = self._manager.acquire(request_key=key, tier=tier, owner=self._owner)
            if allocation.state != "DESTROYED":
                return allocation
        raise ContractViolation(ErrorCode.CONFLICT, "no reusable sandbox allocation for this step")

    def _run(
        self,
        request: ToolRequest,
        *,
        scope: RunScope,
        allocation: SandboxAllocation,
        order_id: str,
    ) -> ArtifactReference:
        self._manager.begin(allocation, owner=self._owner)
        content = self._answered(request, order_id=order_id, tenant_id=scope.tenant_id)
        if len(content) > self._max_result_bytes:
            raise ContractViolation(
                ErrorCode.BUDGET_EXHAUSTED, "tool result exceeds the sandbox result budget"
            )
        record = self._manager.publish(
            allocation, owner=self._owner, name=f"{request.call_id}.json", content=content
        )
        digest = sha256(content).hexdigest()
        if record.sha256 != digest:
            # A real backend hashes what it actually stored; a mismatch means
            # the sandbox did not receive the bytes this step believes it sent.
            raise ContractViolation(ErrorCode.CONFLICT, "sandbox artifact digest mismatch")
        return self._store.put(
            scope, reference_id=f"tool-result:{request.call_id}", content=content
        )

    def _answered(self, request: ToolRequest, *, order_id: str, tenant_id: str) -> bytes:
        arguments = _revalidated(request)
        if isinstance(arguments, MaterialDraftArguments):
            body: dict[str, object] = {
                "schema": MATERIAL_DRAFT_SCHEMA,
                "order_id": order_id,
                "questions": list(arguments.questions),
            }
            return json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        if request.name == "lookup_order":
            return self._connector.lookup_order(tenant_id=tenant_id, order_id=order_id).content()
        if request.name == "lookup_tracking":
            return self._connector.lookup_tracking(tenant_id=tenant_id, order_id=order_id).content()
        raise ContractViolation(ErrorCode.FORBIDDEN, "tool has no connector binding")

    def _release_after_failure(self, allocation: SandboxAllocation) -> None:
        try:
            self._manager.release(allocation, owner=self._owner)
        except ContractViolation:
            # Deliberately swallowed: it would replace the real failure, and an
            # unconfirmed sandbox is reclaimed by the lease sweep anyway.
            return
