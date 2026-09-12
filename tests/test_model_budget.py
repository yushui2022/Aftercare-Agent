import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.model_adapters import ResponseUsage
from aftercare_agent.model_adapters.budget import ModelPricing, ModelUsageBudget
from aftercare_agent.model_adapters.responses import ResponsesAdapter, ResponsesRequest


def test_usage_budget_charges_integer_cost_and_rejects_overage() -> None:
    budget = ModelUsageBudget(
        input_tokens_remaining=100,
        output_tokens_remaining=50,
        cost_microusd_remaining=1_000,
    )
    updated = budget.charge(
        ResponseUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        ModelPricing(input_microusd_per_token=20, output_microusd_per_token=40),
    )
    assert updated.input_tokens_remaining == 90
    assert updated.output_tokens_remaining == 45
    assert updated.cost_microusd_remaining == 600
    with pytest.raises(ContractViolation) as error:
        updated.charge(
            ResponseUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            ModelPricing(input_microusd_per_token=1, output_microusd_per_token=1_000),
        )
    assert error.value.code is ErrorCode.BUDGET_EXHAUSTED


class _Endpoint:
    def create(self, **kwargs: object) -> object:
        return {
            "id": "resp-budget",
            "status": "completed",
            "output": [],
            "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        }


class _Client:
    responses = _Endpoint()


def test_adapter_budgeted_call_requires_usage_and_returns_next_snapshot() -> None:
    adapter = ResponsesAdapter(_Client(), allowed_tools=frozenset())
    response, remaining = adapter.complete_with_budget(
        ResponsesRequest(model="model-1", input="hello"),
        ModelUsageBudget(
            input_tokens_remaining=10,
            output_tokens_remaining=10,
            cost_microusd_remaining=100,
        ),
        ModelPricing(input_microusd_per_token=3, output_microusd_per_token=4),
    )
    assert response.response_id == "resp-budget"
    assert remaining.cost_microusd_remaining == 90
