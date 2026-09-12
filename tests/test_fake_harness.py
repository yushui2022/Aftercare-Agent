from datetime import UTC, datetime

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import Checkpoint
from aftercare_agent.runtime import run_fake_harness

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


def test_fake_harness_runs_fixed_readonly_plan_and_can_resume() -> None:
    first = run_fake_harness(
        tenant_id="tenant-1", case_id="case-1", run_id="run-1", now=NOW, max_steps=2
    )
    assert not first.completed
    assert first.tool_calls == 1
    assert first.checkpoint.next_step == "tool"
    resumed = run_fake_harness(
        tenant_id="tenant-1",
        case_id="case-1",
        run_id="run-1",
        checkpoint=first.checkpoint,
        now=NOW,
    )
    assert resumed.completed
    assert resumed.tool_calls == 3
    assert resumed.checkpoint.next_step == "complete"
    assert len(resumed.checkpoint.tool_results) == 3
    assert (
        Checkpoint.model_validate_json(resumed.checkpoint.model_dump_json()) == resumed.checkpoint
    )


def test_fake_harness_rejects_cross_scope_resume() -> None:
    first = run_fake_harness(
        tenant_id="tenant-1", case_id="case-1", run_id="run-1", now=NOW, max_steps=1
    )
    with pytest.raises(ContractViolation) as error:
        run_fake_harness(
            tenant_id="tenant-2",
            case_id="case-1",
            run_id="run-1",
            checkpoint=first.checkpoint,
            now=NOW,
        )
    assert error.value.code is ErrorCode.FORBIDDEN


def test_fake_harness_bounds_steps_and_budget() -> None:
    with pytest.raises(ContractViolation) as error:
        run_fake_harness(
            tenant_id="tenant-1", case_id="case-1", run_id="run-1", now=NOW, max_steps=0
        )
    assert error.value.code is ErrorCode.INVALID_INPUT
