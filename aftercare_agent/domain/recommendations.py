"""Durable, provider-neutral snapshots of investigation assessments."""

import hashlib
import json

from pydantic import Field

from .common import Identifier, RunScope, SchemaVersion, Sha256, UtcDatetime
from .investigation import InvestigationAssessment


class InvestigationAssessmentRecord(RunScope):
    """An immutable assessment result, never an external-action grant."""

    schema_version: SchemaVersion = 1
    assessment_id: Identifier
    assessment_sha256: Sha256
    policy_id: Identifier
    policy_version: int = Field(strict=True, ge=1)
    disposition: str
    assessment: InvestigationAssessment
    created_at: UtcDatetime


def assessment_digest(assessment: InvestigationAssessment) -> str:
    """Hash the complete canonical result, including citations and timestamps."""
    encoded = json.dumps(
        assessment.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["InvestigationAssessmentRecord", "assessment_digest"]
