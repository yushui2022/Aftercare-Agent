"""Trusted investigation adapters over the deterministic domain contracts."""

from .connector_bridge import (
    ConnectorEvidenceRecorder,
    evidence_from_answer,
    registered_source_kinds,
)
from .egm import InvestigationEvidenceAdapter

__all__ = [
    "ConnectorEvidenceRecorder",
    "InvestigationEvidenceAdapter",
    "evidence_from_answer",
    "registered_source_kinds",
]
