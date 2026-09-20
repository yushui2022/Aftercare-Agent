"""Trusted investigation adapters over the deterministic domain contracts."""

from .connector_bridge import (
    ConnectorEvidenceRecorder,
    RunScopedEvidenceRecorder,
    evidence_from_answer,
    registered_source_kinds,
    render_evidence_context,
)
from .egm import (
    InvestigationEvidenceAdapter,
    build_investigation_application,
    investigation_schema,
)
from .source_ingress import AuthenticatedSourceIngress

__all__ = [
    "ConnectorEvidenceRecorder",
    "AuthenticatedSourceIngress",
    "InvestigationEvidenceAdapter",
    "build_investigation_application",
    "investigation_schema",
    "RunScopedEvidenceRecorder",
    "evidence_from_answer",
    "registered_source_kinds",
    "render_evidence_context",
]
