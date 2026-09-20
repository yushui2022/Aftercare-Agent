"""Deterministic closed-loop evaluation of the bounded investigation Harness.

Unlike :mod:`evals.runner`, which starts from an already-built proposal, this
evaluator enters through the real model boundary: a scripted Responses client
returns one provider-shaped ``function_call``, the adapter normalizes it under a
tool whitelist, and the integer budget charges the reported usage.  The proposal
then goes through the same deterministic assessment, and each result records the
routing decision, citations and cost.

Nothing here calls a network, database, connector, sandbox or real model, so the
numbers are engineering regression evidence with synthetic pricing.  They are not
model quality, provider billing or production SLA claims.  Uncertain or
conflicting cases must still route away from ``RECOMMENDATION_READY``, and no
assessment may authorize an external action or close a case.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Final

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import (
    FreshnessPolicy,
    InvestigationAssessment,
    InvestigationDisposition,
    InvestigationEvidence,
    InvestigationProposal,
    assess_investigation,
    parse_investigation_proposal,
)
from aftercare_agent.model_adapters.budget import ModelPricing, ModelUsageBudget
from aftercare_agent.model_adapters.responses import (
    NormalizedResponse,
    ResponsesAdapter,
    ResponsesRequest,
    ToolSpec,
)
from aftercare_agent.runtime.harness import run_fake_harness
from evals.cases.catalog import NOW, POLICY, REGISTRY, SCOPE, catalogue
from evals.runner import assess_proposal, expected_case

EVALUATOR_VERSION: Final = "a3-04-v1.1"
PROPOSAL_TOOL: Final = "submit_investigation_proposal"
MODEL_ID: Final = "synthetic-loop-model"

# Synthetic, round-number prices.  Real per-token prices come from the provider
# and must be calibrated against actual billing before any cost claim is made.
SYNTHETIC_PRICING: Final = ModelPricing(
    input_microusd_per_token=3,
    output_microusd_per_token=15,
)

SYNTHETIC_BUDGET: Final = ModelUsageBudget(
    input_tokens_remaining=1_000_000,
    output_tokens_remaining=100_000,
    cost_microusd_remaining=10_000_000,
)


@dataclass(frozen=True)
class LoopCaseResult:
    case_id: str
    passed: bool
    outcome: str
    disposition: str | None
    routed_to_human_review: bool
    citations: tuple[str, ...]
    uncited_acceptances: int
    unknown_citations: tuple[str, ...]
    error_code: str | None
    input_tokens: int
    output_tokens: int
    cost_microusd: int


@dataclass(frozen=True)
class LoopReport:
    evaluator: str
    results: tuple[LoopCaseResult, ...]
    recommendations: int
    routed_to_review: int
    boundary_failures: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_microusd: int
    loop_steps: int
    loop_tool_calls: int
    loop_completed: bool
    loop_resumed: bool


class ScriptedResponsesClient:
    """Deterministic stand-in for a provider SDK; it never leaves the process."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        self.responses = self
        self.requests: list[dict[str, object]] = []
        self._payload = payload

    def create(self, **kwargs: object) -> Mapping[str, object]:
        self.requests.append(dict(kwargs))
        return self._payload


def _observations(case_id: str) -> tuple[InvestigationEvidence, ...]:
    return catalogue()[case_id][1]


def _proposal_arguments(case_id: str) -> str:
    proposal_input, _ = catalogue()[case_id]
    raw: object = (
        proposal_input.model_dump(mode="json")
        if isinstance(proposal_input, InvestigationProposal)
        else proposal_input
    )
    return json.dumps(raw, ensure_ascii=False, sort_keys=True)


def _usage(case_id: str) -> tuple[int, int]:
    """Return deterministic synthetic token usage for one case."""

    observations = _observations(case_id)
    input_tokens = 40 + 12 * len(observations)
    output_tokens = 16 + len(_proposal_arguments(case_id)) // 4
    return input_tokens, output_tokens


def _payload(case_id: str, input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "id": f"resp-{case_id}",
        "object": "response",
        "created_at": 1,
        "model": MODEL_ID,
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "id": f"fc-{case_id}",
                "call_id": f"call-{case_id}",
                "name": PROPOSAL_TOOL,
                "arguments": _proposal_arguments(case_id),
                "status": "completed",
            }
        ],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _prompt(case_id: str) -> str:
    """A bounded synthetic prompt naming the authorized ledger, not its content."""

    listing = ", ".join(
        f"{item.evidence_id}:{item.source_id}"
        for item in sorted(_observations(case_id), key=lambda evidence: evidence.evidence_id)
    )
    return f"synthetic case {case_id}; authorized observations: {listing}"


def scripted_model_call(case_id: str) -> tuple[NormalizedResponse, Mapping[str, object], int]:
    """Drive the real adapter with a scripted provider payload.

    Returns the normalized response, the exact request the adapter handed to the
    client, and the synthetic cost of that call in micro-USD.
    """

    input_tokens, output_tokens = _usage(case_id)
    client = ScriptedResponsesClient(_payload(case_id, input_tokens, output_tokens))
    adapter = ResponsesAdapter(client, allowed_tools=frozenset({PROPOSAL_TOOL}))
    response, remaining = adapter.complete_with_budget(
        ResponsesRequest(
            model=MODEL_ID, input=_prompt(case_id), tools=(ToolSpec(name=PROPOSAL_TOOL),)
        ),
        SYNTHETIC_BUDGET,
        SYNTHETIC_PRICING,
    )
    cost = SYNTHETIC_BUDGET.cost_microusd_remaining - remaining.cost_microusd_remaining
    return response, client.requests[-1], cost


def _citation_checks(
    assessment: InvestigationAssessment, ledger: Sequence[InvestigationEvidence]
) -> tuple[tuple[str, ...], int, tuple[str, ...]]:
    ledger_ids = {evidence.evidence_id for evidence in ledger}
    citations = tuple(
        sorted(
            {
                citation.evidence_id
                for decision in assessment.decisions
                for citation in decision.citations
            }
        )
    )
    uncited = sum(
        1 for decision in assessment.decisions if decision.accepted and not decision.citations
    )
    unknown = tuple(sorted(set(citations) - ledger_ids))
    return citations, uncited, unknown


def _boundary_failure(
    case_id: str, usage_input: int, usage_output: int, cost: int
) -> LoopCaseResult:
    expected = expected_case(case_id)
    passed = expected.get("outcome") == "error" and expected.get("error_code") == "invalid_input"
    return LoopCaseResult(
        case_id,
        passed,
        "model_boundary_error",
        None,
        False,
        (),
        0,
        (),
        ErrorCode.INVALID_INPUT.value,
        usage_input,
        usage_output,
        cost,
    )


def evaluate_loop_case(case_id: str) -> LoopCaseResult:
    response, _, cost = scripted_model_call(case_id)
    usage = response.usage
    if usage is None:  # pragma: no cover - a budgeted call requires usage
        raise ContractViolation(ErrorCode.INVALID_INPUT, "budgeted call must report usage")
    call = response.tool_calls[0]
    try:
        proposal = parse_investigation_proposal(
            json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
        )
    except ContractViolation:
        return _boundary_failure(case_id, usage.input_tokens, usage.output_tokens, cost)
    assessed = assess_proposal(case_id, proposal)
    if assessed.outcome == "error":
        return LoopCaseResult(
            case_id,
            assessed.passed,
            "error",
            None,
            False,
            (),
            0,
            (),
            assessed.error_code,
            usage.input_tokens,
            usage.output_tokens,
            cost,
        )
    ledger = _observations(case_id)
    assessment = assess_investigation(
        SCOPE,
        proposal,
        ledger,
        REGISTRY,
        FreshnessPolicy.model_validate(POLICY),
        now=NOW,
    )
    citations, uncited, unknown = _citation_checks(assessment, ledger)
    passed = (
        assessed.passed
        and uncited == 0
        and not unknown
        and not assessment.authorizes_external_action
        and not assessment.closes_case
    )
    return LoopCaseResult(
        case_id,
        passed,
        "assessment",
        assessment.disposition.value,
        assessment.disposition is not InvestigationDisposition.RECOMMENDATION_READY,
        citations,
        uncited,
        unknown,
        None,
        usage.input_tokens,
        usage.output_tokens,
        cost,
    )


def _run_bounded_loop() -> tuple[int, int, bool, bool]:
    """Prove the step loop is bounded and resumable from a stored checkpoint."""

    first = run_fake_harness(
        tenant_id="tenant-1", case_id="case-1", run_id="loop-run-1", now=NOW, max_steps=2
    )
    resumed = run_fake_harness(
        tenant_id="tenant-1",
        case_id="case-1",
        run_id="loop-run-1",
        checkpoint=first.checkpoint,
        now=NOW,
        max_steps=8,
    )
    steps = resumed.checkpoint.checkpoint_version - 1
    return steps, resumed.tool_calls, resumed.completed, not first.completed


def evaluate_loop() -> LoopReport:
    results = tuple(evaluate_loop_case(case_id) for case_id in sorted(catalogue()))
    steps, tool_calls, completed, resumed = _run_bounded_loop()
    return LoopReport(
        EVALUATOR_VERSION,
        results,
        sum(1 for result in results if result.disposition == "recommendation_ready"),
        sum(1 for result in results if result.routed_to_human_review),
        sum(1 for result in results if result.outcome == "model_boundary_error"),
        sum(result.input_tokens for result in results),
        sum(result.output_tokens for result in results),
        sum(result.cost_microusd for result in results),
        steps,
        tool_calls,
        completed,
        resumed,
    )


def report_payload(report: LoopReport) -> dict[str, object]:
    payload: dict[str, object] = {
        "evaluator": report.evaluator,
        "recommendations": report.recommendations,
        "routed_to_review": report.routed_to_review,
        "boundary_failures": report.boundary_failures,
        "total_input_tokens": report.total_input_tokens,
        "total_output_tokens": report.total_output_tokens,
        "total_cost_microusd": report.total_cost_microusd,
        "loop_steps": report.loop_steps,
        "loop_tool_calls": report.loop_tool_calls,
        "loop_completed": report.loop_completed,
        "loop_resumed": report.loop_resumed,
        "results": [
            {field.name: getattr(result, field.name) for field in fields(result)}
            for result in report.results
        ],
    }
    return payload


def loop_digest(report: LoopReport | None = None) -> str:
    current = report if report is not None else evaluate_loop()
    payload = json.dumps(report_payload(current), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    report = evaluate_loop()
    for result in report.results:
        status = "PASS" if result.passed else "FAIL"
        detail = result.disposition or result.error_code or ""
        sources = ",".join(result.citations) if result.citations else "-"
        print(f"{status} {result.case_id}: {detail} citations={sources}")
    print(
        f"recommendations={report.recommendations} routed_to_review={report.routed_to_review} "
        f"boundary_failures={report.boundary_failures}"
    )
    print(
        f"input_tokens={report.total_input_tokens} output_tokens={report.total_output_tokens} "
        f"cost_microusd={report.total_cost_microusd} (synthetic pricing)"
    )
    print(
        f"loop steps={report.loop_steps} tool_calls={report.loop_tool_calls} "
        f"completed={report.loop_completed} resumed={report.loop_resumed}"
    )
    print(f"digest={loop_digest(report)}")
    return 0 if all(result.passed for result in report.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
