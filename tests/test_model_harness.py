"""Invariants of the model-driven Harness.

Only the properties that cost money or break recovery are asserted here: a
resumed slice must not pay for a second model turn, an unvalidated intent must
never reach the executor, and the persisted budget must stop the loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from aftercare_agent.domain.common import ContractViolation, ErrorCode, RunScope
from aftercare_agent.domain.protocol import ArtifactReference, Checkpoint, ToolRequest
from aftercare_agent.model_adapters.budget import ModelPricing
from aftercare_agent.model_adapters.responses import ResponsesAdapter, ToolSpec
from aftercare_agent.model_adapters.transport import ProviderError
from aftercare_agent.runtime.harness import HarnessResult
from aftercare_agent.runtime.model_harness import HarnessLimits, run_model_harness

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
PRICING = ModelPricing(input_microusd_per_token=1, output_microusd_per_token=1)
LIMITS = HarnessLimits(model_calls=2, tool_calls=2, cost_microusd=10_000, ttl=timedelta(hours=1))
BACKOFF = timedelta(seconds=30)
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
PROPOSAL = (
    '{"schema_version":1,"claims":[{"claim":"order_recorded","evidence_refs":["evidence-1"]}]}'
)


def _envelope(*items: dict[str, object], usage: dict[str, int] | None = None) -> dict[str, object]:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": list(items),
        "usage": usage or {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _tool_call(name: str, arguments: str, *, call_id: str = "call_1") -> dict[str, object]:
    return {
        "type": "function_call",
        "call_id": call_id,
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


def _run(client: object, executor: RecordingExecutor, **overrides: object) -> HarnessResult:
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


def test_before_run_syncs_the_bound_case_before_spending_model_budget() -> None:
    scopes: list[RunScope] = []
    client = ScriptedClient(_envelope(_text("done")))
    result = _run(client, RecordingExecutor(), before_run=scopes.append, max_steps=1)
    assert scopes == [RunScope(tenant_id="tenant-a", case_id="case-a", run_id="run-a")]
    assert len(client.calls) == 1
    assert result.checkpoint.remaining_budget.model_calls == LIMITS.model_calls - 1


def test_a_final_turn_is_saved_as_a_proposal_before_it_can_be_assessed() -> None:
    client = ScriptedClient(_envelope(_text(PROPOSAL)))
    proposed = _run(client, RecordingExecutor(), max_steps=8)
    assert proposed.checkpoint.next_step == "evaluate"
    assert proposed.checkpoint.pending_proposal_json is not None
    assert proposed.proposal is None
    resumed = _run(client, RecordingExecutor(), checkpoint=proposed.checkpoint, max_steps=8)
    assert len(client.calls) == 1, "assessment resume must not buy another model turn"
    assert resumed.proposal is not None
    assert resumed.proposal.claims[0].evidence_refs == ("evidence-1",)


def test_resuming_with_a_changed_model_strategy_routes_to_review() -> None:
    executor = RecordingExecutor()
    client = ScriptedClient(_envelope(_tool_call("lookup_order", "{}")))
    first = _run(client, executor, max_steps=1)
    assert first.checkpoint.strategy_id == "aftercare-investigation"
    changed = _run(
        client,
        executor,
        checkpoint=first.checkpoint,
        model="provider/model-b",
        max_steps=1,
    )
    assert changed.checkpoint.next_step == "review"
    assert changed.checkpoint.route_reason == "model_strategy_changed"
    assert len(client.calls) == 1


def test_free_form_final_text_routes_to_review_instead_of_bypassing_the_gate() -> None:
    result = _run(
        ScriptedClient(_envelope(_text("the parcel was delivered"))),
        RecordingExecutor(),
        max_steps=8,
    )
    assert result.checkpoint.next_step == "review"
    assert result.checkpoint.route_reason == "proposal_rejected"


def test_retryable_before_run_failure_routes_without_calling_the_model() -> None:
    def fail(scope: RunScope) -> None:
        del scope
        raise ContractViolation(ErrorCode.RETRYABLE, "source unavailable")

    client = ScriptedClient(_envelope(_text("must not run")))
    result = _run(client, RecordingExecutor(), before_run=fail, max_steps=1)
    assert client.calls == []
    assert result.checkpoint.next_step == "retry"
    assert result.checkpoint.route_reason == "executor_rejected"
    assert result.checkpoint.available_at == NOW + BACKOFF
    assert result.checkpoint.remaining_budget.model_calls == LIMITS.model_calls


def test_input_render_failure_does_not_charge_a_model_call_that_never_happened() -> None:
    def fail(checkpoint: Checkpoint) -> str:
        del checkpoint
        raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "evidence context is too large")

    client = ScriptedClient(_envelope(_text("must not run")))
    result = _run(client, RecordingExecutor(), render_input=fail, max_steps=1)
    assert client.calls == []
    assert result.checkpoint.next_step == "review"
    assert result.checkpoint.route_reason == "input_rejected"
    budget = result.checkpoint.remaining_budget
    assert budget.model_calls == LIMITS.model_calls
    assert budget.tool_calls == LIMITS.tool_calls
    assert budget.cost_microusd == LIMITS.cost_microusd


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
    proposed = _run(client, executor, max_steps=3)
    assert proposed.checkpoint.next_step == "tool"
    result = _run(client, executor, checkpoint=proposed.checkpoint, max_steps=3)
    assert executor.requests == []
    checkpoint = result.checkpoint
    # The refusal is a durable route, not an exception: the Run has to leave
    # RUNNING where an operator can see it, and the turn that produced the bad
    # intent is already paid for.
    assert checkpoint.next_step == "review"
    assert checkpoint.route_reason == "intent_rejected"
    assert checkpoint.pending_tool is None
    assert checkpoint.remaining_budget.model_calls == LIMITS.model_calls - 1


def test_a_spent_model_budget_routes_to_review_instead_of_raising() -> None:
    executor = RecordingExecutor()
    client = ScriptedClient(
        _envelope(_tool_call("lookup_order", "{}", call_id="call_1")),
        _envelope(_tool_call("lookup_order", "{}", call_id="call_2")),
    )
    result = _run(client, executor, max_steps=9)
    result = _run(client, executor, checkpoint=result.checkpoint, max_steps=9)
    result = _run(client, executor, checkpoint=result.checkpoint, max_steps=9)
    result = _run(client, executor, checkpoint=result.checkpoint, max_steps=9)
    result = _run(client, executor, checkpoint=result.checkpoint, max_steps=9)
    assert len(client.calls) == LIMITS.model_calls
    checkpoint = result.checkpoint
    assert checkpoint.next_step == "review"
    assert checkpoint.route_reason == "model_budget_exhausted"
    assert checkpoint.remaining_budget.model_calls == 0
    assert result.completed is False


def test_a_turn_with_neither_text_nor_a_tool_call_routes_to_review() -> None:
    executor = RecordingExecutor()
    client = ScriptedClient(_envelope())
    result = _run(client, executor, max_steps=2)
    assert result.checkpoint.next_step == "review"
    assert result.checkpoint.route_reason == "empty_turn"


class FailingClient:
    """A provider that refuses the call with a status the normaliser classifies."""

    def __init__(self, status_code: int) -> None:
        self.responses = self
        self.calls: list[dict[str, object]] = []
        self.status_code = status_code

    def create(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(dict(kwargs))
        raise ProviderError("provider refused the call", status_code=self.status_code)


def test_a_retryable_provider_failure_parks_the_run_without_inventing_a_cost() -> None:
    executor = RecordingExecutor()
    client = FailingClient(429)
    checkpoint = _run(client, executor, max_steps=2).checkpoint
    assert checkpoint.next_step == "retry"
    assert checkpoint.route_reason == "provider_retryable_error"
    assert checkpoint.available_at == NOW + BACKOFF
    assert checkpoint.remaining_budget.model_calls == LIMITS.model_calls - 1
    # No usage was reported, so no token cost is claimed: the attempt is
    # charged, the money is not guessed.
    assert checkpoint.remaining_budget.cost_microusd == LIMITS.cost_microusd


def test_a_tool_turn_the_budget_cannot_pay_for_routes_to_review() -> None:
    executor = RecordingExecutor()
    client = ScriptedClient(_envelope(_tool_call("lookup_order", "{}")))
    limits = HarnessLimits(
        model_calls=2, tool_calls=0, cost_microusd=10_000, ttl=timedelta(hours=1)
    )
    checkpoint = _run(client, executor, max_steps=3, limits=limits).checkpoint
    assert checkpoint.next_step == "review"
    assert checkpoint.route_reason == "tool_budget_exhausted"
    assert checkpoint.pending_tool is None
    assert executor.requests == []
    assert checkpoint.remaining_budget.model_calls == limits.model_calls - 1
