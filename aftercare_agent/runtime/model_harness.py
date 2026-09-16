"""A bounded Harness whose next move comes from a real model, not a fixed list.

The same boundaries as the fake Harness hold here: the caller owns the lease
and the transactions, this module never opens one, and every loop is bounded
by budget that is persisted in the checkpoint rather than kept in memory.  The
model may only *propose*; a returned tool intent is re-validated against the
current schema before a trusted executor is allowed to run it.

Tool execution is a seam on purpose.  This module owns "what the model asked
for and whether it is allowed"; the connector layer owns "what the business
system answers".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope, utc
from aftercare_agent.domain.protocol import (
    ArtifactReference,
    Checkpoint,
    RemainingBudget,
    ToolRequest,
    ToolResultReference,
    validate_tool_request,
)
from aftercare_agent.model_adapters.budget import ModelPricing
from aftercare_agent.model_adapters.responses import (
    NormalizedResponse,
    ResponsesAdapter,
    ResponsesInput,
    ResponsesRequest,
    ToolSpec,
)

from .harness import HarnessResult

MODEL_HARNESS_DEFINITION = "model-v1"

type InputRenderer = Callable[[Checkpoint], ResponsesInput]


@runtime_checkable
class ToolExecutor(Protocol):
    """Trusted execution of one already-validated read-only tool intent."""

    def execute(self, request: ToolRequest, *, scope: RunScope, now: datetime) -> ArtifactReference:
        """Run the intent and return an immutable reference to its result."""


@dataclass(frozen=True)
class HarnessLimits:
    """Starting budget for a fresh Run; every field is persisted, not held in memory."""

    model_calls: int
    tool_calls: int
    cost_microusd: int
    ttl: timedelta


def _initial_checkpoint(
    *,
    tenant_id: str,
    case_id: str,
    run_id: str,
    now: datetime,
    limits: HarnessLimits,
    model_config_version: str,
) -> Checkpoint:
    return Checkpoint(
        tenant_id=tenant_id,
        case_id=case_id,
        run_id=run_id,
        checkpoint_version=1,
        input_version=1,
        case_version=1,
        saved_fencing_token=1,
        definition_version=MODEL_HARNESS_DEFINITION,
        policy_version="policy-v1",
        tool_schema_version="tools-v1",
        model_config_version=model_config_version,
        protocol_version="runtime-v1",
        remaining_budget=RemainingBudget(
            model_calls=limits.model_calls,
            tool_calls=limits.tool_calls,
            cost_microusd=limits.cost_microusd,
            deadline=utc(now) + limits.ttl,
        ),
        next_step="model",
    )


def _charge(response: NormalizedResponse, pricing: ModelPricing, budget: RemainingBudget) -> int:
    """Return the cost of one answered call, refusing to guess a missing usage."""

    if response.usage is None:
        raise ContractViolation(
            ErrorCode.INVALID_INPUT, "a chargeable model call must report usage"
        )
    return (
        response.usage.input_tokens * pricing.input_microusd_per_token
        + response.usage.output_tokens * pricing.output_microusd_per_token
    )


def _model_step(
    current: Checkpoint,
    *,
    adapter: ResponsesAdapter,
    model: str,
    tools: tuple[ToolSpec, ...],
    pricing: ModelPricing,
    render_input: InputRenderer,
    now: datetime,
) -> Checkpoint:
    budget = current.remaining_budget
    if budget.model_calls < 1:
        raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "model budget exhausted")
    if utc(now) >= budget.deadline:
        raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "model deadline passed")
    response = adapter.complete(
        ResponsesRequest(model=model, input=render_input(current), tools=tools)
    )
    cost = _charge(response, pricing, budget)
    if cost > budget.cost_microusd:
        raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "model cost budget exhausted")
    charged = budget.model_copy(
        update={"model_calls": budget.model_calls - 1, "cost_microusd": budget.cost_microusd - cost}
    )
    if len(response.tool_calls) > 1:
        # One intent per turn keeps the checkpoint resumable without a queue,
        # and stops a single turn from spending several tool slots at once.
        raise ContractViolation(ErrorCode.INVALID_INPUT, "one tool call per turn is supported")
    if response.tool_calls:
        call = response.tool_calls[0]
        return current.model_copy(
            update={
                "checkpoint_version": current.checkpoint_version + 1,
                "remaining_budget": charged,
                "next_step": "tool",
                "pending_tool": ToolRequest(
                    call_id=call.call_id,
                    name=call.name,
                    arguments_json=json.dumps(
                        dict(call.arguments),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            }
        )
    if not response.text:
        raise ContractViolation(
            ErrorCode.INVALID_INPUT, "model returned neither text nor a tool call"
        )
    return current.model_copy(
        update={
            "checkpoint_version": current.checkpoint_version + 1,
            "remaining_budget": charged,
            "next_step": "complete",
        }
    )


def _tool_step(
    current: Checkpoint,
    *,
    executor: ToolExecutor,
    allowed_tools: frozenset[str],
    now: datetime,
) -> Checkpoint:
    pending = current.pending_tool
    if pending is None:
        raise ContractViolation(ErrorCode.CONFLICT, "tool step without a pending tool")
    budget = current.remaining_budget
    # A checkpoint is durable input, not proof that the intent was already
    # checked: re-validate it against the schema and budget in force *now*.
    validate_tool_request(
        pending,
        stream_complete=True,
        allowed_tools=allowed_tools,
        budget=budget,
        now=now,
    )
    artifact = executor.execute(pending, scope=current, now=utc(now))
    index = len(current.tool_results) + 1
    result = ToolResultReference(
        call_id=pending.call_id,
        step_id=f"tool-step-{index}",
        attempt_id=f"tool-attempt-{index}",
        outcome="succeeded",
        artifact=artifact,
    )
    return current.model_copy(
        update={
            "checkpoint_version": current.checkpoint_version + 1,
            "tool_results": (*current.tool_results, result),
            "remaining_budget": budget.model_copy(update={"tool_calls": budget.tool_calls - 1}),
            "next_step": "model",
            "pending_tool": None,
        }
    )


def run_model_harness(
    *,
    tenant_id: str,
    case_id: str,
    run_id: str,
    checkpoint: Checkpoint | None = None,
    now: datetime,
    max_steps: int = 8,
    adapter: ResponsesAdapter,
    model: str,
    tools: tuple[ToolSpec, ...],
    limits: HarnessLimits,
    executor: ToolExecutor,
    render_input: InputRenderer,
    pricing: ModelPricing,
) -> HarnessResult:
    """Advance a model-driven plan by at most ``max_steps`` bounded phases.

    Returns on ``max_steps`` as well as on completion so the caller can give
    the lease back; the returned checkpoint is always resumable, including the
    exact tool that was pending when the slice ran out of steps.
    """

    if max_steps < 1:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "max_steps must be positive")
    current = checkpoint or _initial_checkpoint(
        tenant_id=tenant_id,
        case_id=case_id,
        run_id=run_id,
        now=now,
        limits=limits,
        model_config_version=model,
    )
    if (current.tenant_id, current.case_id, current.run_id) != (tenant_id, case_id, run_id):
        raise ContractViolation(ErrorCode.FORBIDDEN, "checkpoint scope mismatch")
    allowed_tools = frozenset(tool.name for tool in tools)
    for _ in range(max_steps):
        if current.next_step == "complete":
            return HarnessResult(current, len(current.tool_results), True)
        if current.next_step == "model":
            current = _model_step(
                current,
                adapter=adapter,
                model=model,
                tools=tools,
                pricing=pricing,
                render_input=render_input,
                now=now,
            )
            continue
        if current.next_step == "tool":
            current = _tool_step(current, executor=executor, allowed_tools=allowed_tools, now=now)
            continue
        if current.next_step == "evaluate":
            current = current.model_copy(
                update={
                    "checkpoint_version": current.checkpoint_version + 1,
                    "next_step": "complete",
                }
            )
            continue
        raise ContractViolation(ErrorCode.CONFLICT, "model harness cannot resume this step")
    return HarnessResult(current, len(current.tool_results), current.next_step == "complete")
