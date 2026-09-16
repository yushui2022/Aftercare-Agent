"""Invariants of the model-driven Harness.

Only the properties that cost money or break recovery are asserted here: a
resumed slice must not pay for a second model turn, an unvalidated intent must
never reach the executor, and the persisted budget must stop the loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.protocol import ArtifactReference, Checkpoint, ToolRequest
from aftercare_agent.model_adapters.budget import ModelPricing
from aftercare_agent.model_adapters.responses import ResponsesAdapter, ToolSpec
from aftercare_agent.runtime.harness import HarnessResult
from aftercare_agent.runtime.model_harness import HarnessLimits, run_model_harness

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
PRICING = ModelPricing(input_microusd_per_token=1, output_microusd_per_token=1)
LIMITS = HarnessLimits(model_calls=2, tool_calls=2, cost_microusd=10_000, ttl=timedelta(hours=1))
TOOLS = (
    ToolSpec(name="lookup_order"),
    ToolSpec(
        name="request_material_draft",
        parameters={
            "type": "object",
            "properties": {"questions": {"type": "array"}},
            "required": ["questions"],
        },
    ),
)


def _envelope(*items: dict[str, object], usage: dict[str, int] | None = None) -> dict[str, object]:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": list(items),
        "usage": usage or {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _tool_call(name: str, arguments: str) -> dict[str, object]:
    return {
        "type": "function_call",
        "call_id": "call_1",
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


def _text(text: str) -> dict[str, object]:
    return {
        "type": "message",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }


class ScriptedClient:
    """One scripted provider response per call; it never leaves the process."""

    def __init__(self, *payloads: Mapping[str, object]) -> None:
        self.responses = self
        self.calls: list[dict[str, object]] = []
        self._payloads = list(payloads)

    def create(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(dict(kwargs))
        return self._payloads[min(len(self.calls), len(self._payloads)) - 1]


class RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[ToolRequest] = []

    def execute(self, request: ToolRequest, *, scope: object, now: datetime) -> ArtifactReference:
        del scope, now
        self.requests.append(request)
        return ArtifactReference(
            tenant_id="tenant-a",
            case_id="case-a",
            reference_id=f"result-{request.call_id}",
            sha256="a" * 64,
        )


def _run(client: ScriptedClient, executor: RecordingExecutor, **overrides: object) -> HarnessResult:
    adapter = ResponsesAdapter(client, allowed_tools=frozenset(t.name for t in TOOLS))
    arguments: dict[str, object] = {
        "tenant_id": "tenant-a",
        "case_id": "case-a",
        "run_id": "run-a",
        "now": NOW,
        "adapter": adapter,
        "model": "deepseek-flash",
        "tools": TOOLS,
        "limits": LIMITS,
        "executor": executor,
        "render_input": lambda checkpoint: "investigate",
        "pricing": PRICING,
    }
    arguments.update(overrides)
    return run_model_harness(**arguments)  # type: ignore[arg-type]


def test_a_tool_turn_persists_the_exact_pending_tool_and_its_cost() -> None:
    executor = RecordingExecutor()
    client = ScriptedClient(_envelope(_tool_call("lookup_order", "{}")))
    result = _run(client, executor, max_steps=1)
    checkpoint: Checkpoint = result.checkpoint
    assert checkpoint.next_step == "tool"
    assert checkpoint.pending_tool is not None
    assert checkpoint.pending_tool.name == "lookup_order"
    assert checkpoint.remaining_budget.model_calls == LIMITS.model_calls - 1
    assert checkpoint.remaining_budget.cost_microusd == LIMITS.cost_microusd - 15
    assert executor.requests == [], "the model proposed; nothing may run before the tool phase"


def test_resuming_a_pending_tool_does_not_pay_for_a_second_model_turn() -> None:
    client = ScriptedClient(_envelope(_tool_call("lookup_order", "{}")), _envelope(_text("done")))
    executor = RecordingExecutor()
    first = _run(client, executor, max_steps=1)
    assert len(client.calls) == 1
    assert first.checkpoint.remaining_budget.model_calls == LIMITS.model_calls - 1
    # A crash right after the model turn must resume by running the tool it
    # already paid for, not by asking the model the same question again.
    second = _run(client, executor, checkpoint=first.checkpoint, max_steps=1)
    assert len(client.calls) == 1, "resume must not re-ask the model"
    assert [request.name for request in executor.requests] == ["lookup_order"]
    assert second.checkpoint.remaining_budget.model_calls == LIMITS.model_calls - 1
    assert second.checkpoint.next_step == "model"


def test_a_tool_intent_that_violates_its_schema_never_reaches_the_executor() -> None:
    executor = RecordingExecutor()
    # A trusted tool closure binds the order, so a model-supplied order_id is
    # not merely ignored: it is refused before any business call happens.
    client = ScriptedClient(_envelope(_tool_call("lookup_order", '{"order_id": "A-1001"}')))
    with pytest.raises(ContractViolation) as error:
        _run(client, executor, max_steps=3)
    assert error.value.code is ErrorCode.INVALID_INPUT
    assert executor.requests == []


def test_the_persisted_budget_stops_the_loop() -> None:
    executor = RecordingExecutor()
    client = ScriptedClient(_envelope(_tool_call("lookup_order", "{}")))
    with pytest.raises(ContractViolation) as error:
        _run(client, executor, max_steps=9)
    assert error.value.code is ErrorCode.BUDGET_EXHAUSTED
    assert len(client.calls) == LIMITS.model_calls


def test_a_turn_with_neither_text_nor_a_tool_call_is_refused() -> None:
    executor = RecordingExecutor()
    client = ScriptedClient(_envelope())
    with pytest.raises(ContractViolation) as error:
        _run(client, executor, max_steps=2)
    assert error.value.code is ErrorCode.INVALID_INPUT
