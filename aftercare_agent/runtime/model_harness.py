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
from typing import Literal, Protocol, runtime_checkable

from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope, utc
from aftercare_agent.domain.protocol import (
    ArtifactReference,
    Checkpoint,
    RemainingBudget,
    RouteReason,
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
    normalize_error,
)
from aftercare_agent.model_adapters.transport import ProviderError

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
    # How long a retryable provider failure parks the Run.  A retry is not free
    # -- it consumes one model call -- so the budget, not a counter, is what
    # stops a provider outage from being retried forever.
    retry_backoff: timedelta = timedelta(seconds=30)


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


def _cost(response: NormalizedResponse, pricing: ModelPricing) -> int | None:
    """Return the cost of one call, or ``None`` when no usage was reported."""
    if response.usage is None:
        return None
    return (
        response.usage.input_tokens * pricing.input_microusd_per_token
        + response.usage.output_tokens * pricing.output_microusd_per_token
    )


def _spent_one_call(budget: RemainingBudget) -> RemainingBudget:
    """Charge one attempt whose usage the provider never reported.

    A call that failed mid-flight may still have been billed.  Counting the
    attempt under-counts a cost nobody can see, which is safer than treating
    the attempt as free, and it is what bounds a retry loop: the budget runs
    out, the route becomes ``review``, and a human decides.
    """
    return budget.model_copy(update={"model_calls": budget.model_calls - 1})


def _route(
    current: Checkpoint,
    *,
    next_step: Literal["review", "retry"],
    reason: RouteReason,
    budget: RemainingBudget,
    available_at: datetime | None = None,
) -> Checkpoint:
    """Persist a durable decision instead of raising out of the slice.

    Raising would lose the charge of a call that was already billed and leave
    the Run RUNNING until its lease expired, where nothing can act on it.  A
    route travels with the checkpoint, so the Worker can move the Run to a
    state an operator or the queue decides about.
    """
    return current.model_copy(
        update={
            "checkpoint_version": current.checkpoint_version + 1,
            "remaining_budget": budget,
            "next_step": next_step,
            "route_reason": reason,
            "pending_tool": None,
            "available_at": available_at,
        }
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
    retry_backoff: timedelta,
) -> Checkpoint:
    budget = current.remaining_budget
    if utc(now) >= budget.deadline:
        return _route(current, next_step="review", reason="deadline_passed", budget=budget)
    if budget.model_calls < 1:
        return _route(current, next_step="review", reason="model_budget_exhausted", budget=budget)
    try:
        response = adapter.complete(
            ResponsesRequest(model=model, input=render_input(current), tools=tools)
        )
    except (ProviderError, ContractViolation) as exc:
        charged = _spent_one_call(budget)
        info = normalize_error(exc)
        if info.retryable and utc(now) + retry_backoff <= budget.deadline:
            return _route(
                current,
                next_step="retry",
                reason="provider_retryable_error",
                budget=charged,
                available_at=utc(now) + retry_backoff,
            )
        return _route(current, next_step="review", reason="provider_error", budget=charged)
    cost = _cost(response, pricing)
    if cost is None:
        # A budget this Run has to be able to prove cannot be charged against a
        # usage the provider never reported.
        return _route(
            current, next_step="review", reason="empty_turn", budget=_spent_one_call(budget)
        )
    if cost > budget.cost_microusd:
        return _route(
            current,
            next_step="review",
            reason="cost_budget_exhausted",
            budget=_spent_one_call(budget),
        )
    charged = budget.model_copy(
        update={"model_calls": budget.model_calls - 1, "cost_microusd": budget.cost_microusd - cost}
    )
    if len(response.tool_calls) > 1:
        # One intent per turn keeps the checkpoint resumable without a queue,
        # and stops a single turn from spending several tool slots at once.
        return _route(current, next_step="review", reason="intent_rejected", budget=charged)
    if response.tool_calls:
        call = response.tool_calls[0]
        if budget.tool_calls < 1:
            # The model asked for evidence this Run may no longer buy.  The
            # turn is charged either way: it was already paid for.
            return _route(
                current, next_step="review", reason="tool_budget_exhausted", budget=charged
            )
        return current.model_copy(
            update={
                "checkpoint_version": current.checkpoint_version + 1,
                "remaining_budget": charged,
                "next_step": "tool",
                "route_reason": None,
                "available_at": None,
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
        return _route(current, next_step="review", reason="empty_turn", budget=charged)
    return current.model_copy(
        update={
            "checkpoint_version": current.checkpoint_version + 1,
            "remaining_budget": charged,
            "next_step": "complete",
            "route_reason": None,
            "available_at": None,
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
    try:
        validate_tool_request(
            pending,
            stream_complete=True,
            allowed_tools=allowed_tools,
            budget=budget,
            now=now,
        )
    except ContractViolation:
        # A malformed or out-of-scope intent is a planner problem, and nothing
        # ran.  The paid turn is already recorded; a human decides what next.
        return _route(current, next_step="review", reason="intent_rejected", budget=budget)
    try:
        artifact = executor.execute(pending, scope=current, now=utc(now))
    except ContractViolation:
        # Deliberately not retried: a refused intent does not spend the tool
        # budget, so nothing would stop the next claim from asking the same
        # question of the same broken connector, forever.
        return _route(current, next_step="review", reason="executor_rejected", budget=budget)
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
            "route_reason": None,
            "available_at": None,
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
    if current.next_step in ("review", "retry"):
        # Resuming a routed checkpoint means the host made it runnable again: a
        # REVIEW Run has no queue row, and a RETRY_AT Run waits for its
        # available_at, so only an explicit decision brings it back.  Resuming
        # therefore means "try the model phase again" -- returning the same
        # route immediately would make that release a no-op.
        current = current.model_copy(
            update={"next_step": "model", "route_reason": None, "available_at": None}
        )
    allowed_tools = frozenset(tool.name for tool in tools)
    for _ in range(max_steps):
        if current.next_step == "complete":
            return HarnessResult(current, len(current.tool_results), True)
        if current.next_step in ("review", "retry"):
            # A route ends the slice: it is a decision for the Worker and a
            # human, not another phase to run.
            break
        if current.next_step == "model":
            current = _model_step(
                current,
                adapter=adapter,
                model=model,
                tools=tools,
                pricing=pricing,
                render_input=render_input,
                now=now,
                retry_backoff=limits.retry_backoff,
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
