from datetime import UTC, datetime
from unittest.mock import Mock

from aftercare_agent.domain.common import RunScope
from aftercare_agent.domain.investigation import (
    ClaimProposal,
    FreshnessPolicy,
    InvestigationClaim,
    InvestigationProposal,
)
from aftercare_agent.domain.protocol import Checkpoint, RemainingBudget
from aftercare_agent.runtime.judgment import DeterministicEvidenceJudgmentGate


def test_deterministic_gate_derives_scope_from_checkpoint() -> None:
    now = datetime(2026, 9, 20, tzinfo=UTC)
    checkpoint = Checkpoint(
        tenant_id="tenant-1",
        case_id="case-1",
        run_id="run-1",
        checkpoint_version=1,
        input_version=1,
        case_version=1,
        saved_fencing_token=1,
        definition_version="definition-1",
        policy_version="policy-1",
        tool_schema_version="tools-1",
        model_config_version="model-1",
        protocol_version="protocol-1",
        remaining_budget=RemainingBudget(
            model_calls=1, tool_calls=1, cost_microusd=1, deadline=now
        ),
        next_step="evaluate",
    )
    proposal = InvestigationProposal(
        claims=(
            ClaimProposal(
                claim=InvestigationClaim.ORDER_RECORDED,
                evidence_refs=("evidence-1",),
            ),
        )
    )
    policy = FreshnessPolicy(
        policy_id="policy-1",
        policy_version=1,
        order_max_age_seconds=60,
        carrier_max_age_seconds=60,
        buyer_max_age_seconds=60,
    )
    recorder = Mock()
    expected = object()
    recorder.assess_persisted.return_value = expected
    scopes: list[RunScope] = []

    def recorder_for(scope: RunScope) -> Mock:
        scopes.append(scope)
        return recorder

    gate = DeterministicEvidenceJudgmentGate(recorder_for, policy)

    connection = Mock()
    result = gate(connection, checkpoint, proposal, now)

    assert result is expected
    assert scopes == [RunScope(tenant_id="tenant-1", case_id="case-1", run_id="run-1")]
    recorder.assess_persisted.assert_called_once_with(
        connection,
        RunScope(tenant_id="tenant-1", case_id="case-1", run_id="run-1"),
        proposal,
        policy,
        now=now,
    )
