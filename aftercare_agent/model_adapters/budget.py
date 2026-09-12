"""Deterministic model usage and cost budget accounting."""

from typing import Self

from pydantic import Field, model_validator

from aftercare_agent.domain.common import ContractModel, ContractViolation, ErrorCode

from .responses import ResponseUsage


class ModelPricing(ContractModel):
    """Integer micro-USD prices per token; no floating-point billing math."""

    input_microusd_per_token: int = Field(ge=0)
    output_microusd_per_token: int = Field(ge=0)


class ModelUsageBudget(ContractModel):
    input_tokens_remaining: int = Field(ge=0)
    output_tokens_remaining: int = Field(ge=0)
    cost_microusd_remaining: int = Field(ge=0)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.input_tokens_remaining + self.output_tokens_remaining == 0:
            raise ValueError("model budget must allow at least one token")
        return self

    def charge(self, usage: ResponseUsage, pricing: ModelPricing) -> "ModelUsageBudget":
        if usage.input_tokens > self.input_tokens_remaining:
            raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "input token budget exhausted")
        if usage.output_tokens > self.output_tokens_remaining:
            raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "output token budget exhausted")
        cost = (
            usage.input_tokens * pricing.input_microusd_per_token
            + usage.output_tokens * pricing.output_microusd_per_token
        )
        if cost > self.cost_microusd_remaining:
            raise ContractViolation(ErrorCode.BUDGET_EXHAUSTED, "model cost budget exhausted")
        return self.model_copy(
            update={
                "input_tokens_remaining": self.input_tokens_remaining - usage.input_tokens,
                "output_tokens_remaining": self.output_tokens_remaining - usage.output_tokens,
                "cost_microusd_remaining": self.cost_microusd_remaining - cost,
            }
        )
