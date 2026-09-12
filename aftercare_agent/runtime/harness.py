"""A bounded, database-free Harness used before real model/connector wiring."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import (
    ArtifactReference,
    BoundLookupArguments,
    Checkpoint,
    MaterialDraftArguments,
    RemainingBudget,
    ToolRequest,
    ToolResultReference,
    validate_tool_request,
)


@dataclass(frozen=True)
class FakePlanner:
    """Emit a fixed read-only investigation sequence; no model or network call."""

    tools: tuple[str, ...] = ("lookup_order", "lookup_tracking", "request_material_draft")

    def request(self, index: int) -> ToolRequest:
        if index < 0 or index >= len(self.tools):
            raise ContractViolation(ErrorCode.CONFLICT, "fake plan is complete")
        name = self.tools[index]
        arguments_json = (
            '{"questions":["confirm_address"]}' if name == "request_material_draft" else "{}"
        )
        return ToolRequest(
            call_id=f"fake-call-{index + 1}", name=name, arguments_json=arguments_json
        )


@dataclass(frozen=True)
class HarnessResult:
    checkpoint: Checkpoint
    tool_calls: int
    completed: bool


def _artifact(checkpoint: Checkpoint, call_id: str) -> ArtifactReference:
    digest = hashlib.sha256(call_id.encode("utf-8")).hexdigest()
    return ArtifactReference(
        tenant_id=checkpoint.tenant_id,
        case_id=checkpoint.case_id,
        reference_id=f"fake-result-{call_id}",
        sha256=digest,
    )


def _initial_checkpoint(*, tenant_id: str, case_id: str, run_id: str, now: datetime) -> Checkpoint:
    return Checkpoint(
        tenant_id=tenant_id,
        case_id=case_id,
        run_id=run_id,
        checkpoint_version=1,
        input_version=1,
        case_version=1,
        saved_fencing_token=1,
        definition_version="fake-v1",
        policy_version="fake-policy-v1",
        tool_schema_version="fake-tools-v1",
        model_config_version="fake-model-v1",
        protocol_version="runtime-v1",
        remaining_budget=RemainingBudget(
            model_calls=1,
            tool_calls=3,
            cost_microusd=0,
            deadline=now.replace(tzinfo=UTC) + timedelta(hours=1),
        ),
        next_step="model",
    )


def run_fake_harness(
    *,
    tenant_id: str,
    case_id: str,
    run_id: str,
    checkpoint: Checkpoint | None = None,
    now: datetime,
    max_steps: int = 8,
    planner: FakePlanner | None = None,
) -> HarnessResult:
    """Advance a fixed plan, returning a checkpoint suitable for later resume.

    The function deliberately returns after ``max_steps`` and never holds a DB
    transaction.  It simulates tool results as immutable references; a real
    Harness will replace this planner and connector boundary in A1/A3.
    """
    if max_steps < 1:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "max_steps must be positive")
    current = checkpoint or _initial_checkpoint(
        tenant_id=tenant_id, case_id=case_id, run_id=run_id, now=now
    )
    if (current.tenant_id, current.case_id, current.run_id) != (tenant_id, case_id, run_id):
        raise ContractViolation(ErrorCode.FORBIDDEN, "checkpoint scope mismatch")
    plan = planner or FakePlanner()
    calls = len(current.tool_results)
    for _ in range(max_steps):
        if current.next_step == "complete":
            return HarnessResult(current, calls, True)
        budget = current.remaining_budget
        if current.next_step == "model":
            if budget.model_calls < 1:
                raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "model budget exhausted")
            current = current.model_copy(
                update={
                    "checkpoint_version": current.checkpoint_version + 1,
                    "remaining_budget": budget.model_copy(
                        update={"model_calls": budget.model_calls - 1}
                    ),
                    "next_step": "tool",
                }
            )
            continue
        if current.next_step != "tool":
            raise ContractViolation(ErrorCode.CONFLICT, "fake harness cannot resume this step")
        request = plan.request(calls)
        arguments = validate_tool_request(
            request,
            stream_complete=True,
            allowed_tools=frozenset(plan.tools),
            budget=budget,
            now=now,
        )
        if request.name != "request_material_draft" and not isinstance(
            arguments, BoundLookupArguments
        ):
            raise ContractViolation(ErrorCode.INVALID_INPUT, "unexpected fake tool arguments")
        if request.name == "request_material_draft" and not isinstance(
            arguments, MaterialDraftArguments
        ):
            raise ContractViolation(ErrorCode.INVALID_INPUT, "unexpected fake tool arguments")
        result = ToolResultReference(
            call_id=request.call_id,
            step_id=f"fake-step-{calls + 1}",
            attempt_id=f"fake-attempt-{calls + 1}",
            outcome="succeeded",
            artifact=_artifact(current, request.call_id),
        )
        next_step = "tool" if calls + 1 < len(plan.tools) else "evaluate"
        current = current.model_copy(
            update={
                "checkpoint_version": current.checkpoint_version + 1,
                "tool_results": (*current.tool_results, result),
                "remaining_budget": budget.model_copy(update={"tool_calls": budget.tool_calls - 1}),
                "next_step": next_step,
            }
        )
        calls += 1
        if next_step == "evaluate":
            current = current.model_copy(
                update={
                    "checkpoint_version": current.checkpoint_version + 1,
                    "next_step": "complete",
                }
            )
    return HarnessResult(current, calls, current.next_step == "complete")
