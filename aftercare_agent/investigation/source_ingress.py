"""Bring authenticated connector events into the normal evidence pipeline."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from aftercare_agent.artifacts import ContentAddressedArtifactStore
from aftercare_agent.connectors.source_auth import SignedSourceRequest, SourceEventVerifier
from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope
from aftercare_agent.domain.investigation import InvestigationEvidence
from aftercare_agent.domain.protocol import ToolRequest

from .connector_bridge import RunScopedEvidenceRecorder


class AuthenticatedSourceIngress:
    """Authenticate, scope, store and record one normalized source event."""

    def __init__(
        self,
        *,
        verifier: SourceEventVerifier,
        recorder: RunScopedEvidenceRecorder,
        store: ContentAddressedArtifactStore,
    ) -> None:
        self._verifier = verifier
        self._recorder = recorder
        self._store = store

    def ingest(
        self,
        request: SignedSourceRequest,
        *,
        scope: RunScope,
        now: datetime,
    ) -> InvestigationEvidence:
        event = self._verifier.verify(request, now=now)
        if event.identity.tenant_id != scope.tenant_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "source key belongs to another tenant")
        expected_source = self._recorder.registered_sources.get(event.identity.tool)
        if expected_source != event.identity.source_id:
            raise ContractViolation(
                ErrorCode.FORBIDDEN, "source key is not registered by connector"
            )
        address = sha256(f"{event.identity.source_id}\n{request.delivery_id}".encode()).hexdigest()
        artifact = self._store.put(
            scope,
            reference_id=f"source-event:{address}",
            content=request.content,
        )
        return self._recorder(
            request=ToolRequest(
                call_id=f"source-event:{address}",
                name=event.answer.tool,
                arguments_json="{}",
            ),
            scope=scope,
            answer=event.answer,
            artifact=artifact,
        )


__all__ = ["AuthenticatedSourceIngress"]
