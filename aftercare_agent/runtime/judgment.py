"""Provider-neutral judgment gate seams.

Model providers may propose an investigation, but the Worker calls a
``JudgmentGate`` that belongs to the trusted host.  The default implementation
reads the persisted evidence ledger and delegates to the deterministic EGM
assessment; a future shadow or policy implementation can replace the gate
without giving a model authority over evidence, tenant scope, or actions.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import psycopg

from aftercare_agent.domain.common import RunScope
from aftercare_agent.domain.investigation import (
    FreshnessPolicy,
    InvestigationAssessment,
    InvestigationProposal,
)
from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.investigation import RunScopedEvidenceRecorder


class JudgmentGate(Protocol):
    """Trusted boundary that turns a model proposal into a business assessment.

    Implementations must derive scope from the admitted checkpoint and must
    return an assessment backed by host-authorized evidence.  The protocol has
    no provider, credential, or action method by design.
    """

    def __call__(
        self,
        connection: psycopg.Connection[Any],
        checkpoint: Checkpoint,
        proposal: InvestigationProposal,
        now: datetime,
    ) -> InvestigationAssessment: ...


@dataclass(frozen=True)
class DeterministicEvidenceJudgmentGate:
    """Evaluate proposals against the persisted, run-scoped evidence ledger."""

    recorder_for: Callable[[RunScope], RunScopedEvidenceRecorder]
    policy: FreshnessPolicy

    def __call__(
        self,
        connection: psycopg.Connection[Any],
        checkpoint: Checkpoint,
        proposal: InvestigationProposal,
        now: datetime,
    ) -> InvestigationAssessment:
        scope = RunScope(
            tenant_id=checkpoint.tenant_id,
            case_id=checkpoint.case_id,
            run_id=checkpoint.run_id,
        )
        return self.recorder_for(scope).assess_persisted(
            connection,
            scope,
            proposal,
            self.policy,
            now=now,
        )


__all__ = ["DeterministicEvidenceJudgmentGate", "JudgmentGate"]
