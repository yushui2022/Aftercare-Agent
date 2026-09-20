"""The offline closed-loop evaluator stays deterministic, cited and bounded."""

import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.model_adapters.responses import (
    ResponsesAdapter,
    ResponsesRequest,
    ToolSpec,
)
from evals.loop import (
    PROPOSAL_TOOL,
    SYNTHETIC_PRICING,
    ScriptedResponsesClient,
    evaluate_loop,
    loop_digest,
    scripted_model_call,
)


def test_every_case_passes_through_the_model_boundary() -> None:
    report = evaluate_loop()
    assert len(report.results) == 14
    assert all(result.passed for result in report.results)


def test_loop_digest_is_stable_within_and_across_processes() -> None:
    assert loop_digest() == loop_digest()
    root = Path(__file__).parents[2]
    output = subprocess.check_output([sys.executable, "-m", "evals.loop"], cwd=root, text=True)
    match = re.search(r"^digest=([0-9a-f]{64})$", output, flags=re.MULTILINE)
    assert match is not None
    assert match.group(1) == loop_digest()


def test_recommendations_are_cited_and_uncertainty_routes_to_review() -> None:
    report = evaluate_loop()
    assert report.recommendations == 1
    assert report.routed_to_review == 8
    for result in report.results:
        assert not result.uncited_acceptances
        assert not result.unknown_citations
        if result.disposition == "recommendation_ready":
            assert result.citations
        if result.routed_to_human_review:
            assert result.disposition in {"human_review", "needs_material"}


def test_structured_failures_never_become_recommendations() -> None:
    failures = {
        result.case_id: result.error_code
        for result in evaluate_loop().results
        if result.outcome != "assessment"
    }
    assert failures == {
        "duplicate-evidence": "conflict",
        "duplicate-source-event": "conflict",
        "other-order": "forbidden",
        "other-tenant": "forbidden",
        "prompt-injection": "invalid_input",
    }
    assert not any(
        result.routed_to_human_review
        for result in evaluate_loop().results
        if result.outcome != "assessment"
    )


def test_model_boundary_keeps_store_false_and_the_tool_whitelist() -> None:
    _, request, _ = scripted_model_call("normal")
    assert request["store"] is False
    assert request["input"] == (
        "synthetic case normal; authorized observations: "
        "buyer:buyer-channel, carrier:carrier-api, order:order-api"
    )
    tools = cast(list[dict[str, object]], request["tools"])
    assert [tool["name"] for tool in tools] == [PROPOSAL_TOOL]
    assert all(tool["strict"] is True for tool in tools)


def test_adapter_refuses_a_request_outside_its_tool_whitelist() -> None:
    client = ScriptedResponsesClient({"id": "resp", "status": "completed", "output": []})
    adapter = ResponsesAdapter(client, allowed_tools=frozenset({PROPOSAL_TOOL}))
    with pytest.raises(ContractViolation) as error:
        adapter.complete(
            ResponsesRequest(model="m", input="i", tools=(ToolSpec(name="lookup_order"),))
        )
    assert error.value.code is ErrorCode.FORBIDDEN
    assert not client.requests


def test_synthetic_cost_matches_reported_tokens() -> None:
    report = evaluate_loop()
    assert report.total_input_tokens == sum(result.input_tokens for result in report.results)
    assert report.total_output_tokens == sum(result.output_tokens for result in report.results)
    assert report.total_cost_microusd == sum(result.cost_microusd for result in report.results)
    for result in report.results:
        expected = (
            result.input_tokens * SYNTHETIC_PRICING.input_microusd_per_token
            + result.output_tokens * SYNTHETIC_PRICING.output_microusd_per_token
        )
        assert result.cost_microusd == expected


def test_bounded_loop_resumes_from_a_stored_checkpoint() -> None:
    report = evaluate_loop()
    assert report.loop_resumed is True
    assert report.loop_completed is True
    assert report.loop_tool_calls == 3
    assert report.loop_steps > 0
