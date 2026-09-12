"""Deterministic evaluator for synthetic Aftercare investigation cases.

Usage: ``python -m evals.runner``.  It never calls a model, network, connector,
database, or sandbox.  A failed case is a regression, not a model-quality score.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from pydantic import ValidationError

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.investigation import (
    FreshnessPolicy,
    InvestigationAssessment,
    InvestigationProposal,
    assess_investigation,
)
from evals.cases.catalog import NOW, POLICY, REGISTRY, SCOPE, catalogue

ROOT: Final = Path(__file__).parent
EVALUATOR_VERSION: Final = "a0-03-v1"


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    outcome: str
    disposition: str | None = None
    error_code: str | None = None
    missing: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    unavailable: tuple[tuple[str, str], ...] = ()


def _expected() -> dict[str, dict[str, object]]:
    with (ROOT / "expectations" / "v1.json").open(encoding="utf-8") as stream:
        loaded = cast(dict[str, object], json.load(stream))
        if loaded.pop("schema_version", None) != 1:
            raise ValueError("unsupported expectations schema")
        return cast(dict[str, dict[str, object]], loaded)


def _assessment_result(
    case_id: str, actual: InvestigationAssessment, expected: Mapping[str, object]
) -> CaseResult:
    missing = tuple(item.value for item in actual.missing)
    conflicts = tuple(item.kind.value for item in actual.conflicts)
    unavailable = tuple(
        sorted((item.evidence_id, item.reason.value) for item in actual.unavailable)
    )
    decisions = tuple(
        {
            "claim": decision.claim.value,
            "accepted": decision.accepted,
            "citations": tuple(citation.evidence_id for citation in decision.citations),
            "rejected": tuple(
                {"evidence_id": rejected.evidence_id, "reason": rejected.reason.value}
                for rejected in decision.rejected
            ),
        }
        for decision in actual.decisions
    )
    expected_decisions = tuple(
        {
            "claim": str(item["claim"]),
            "accepted": bool(item["accepted"]),
            "citations": tuple(cast(list[str], item.get("citations", []))),
            "rejected": tuple(cast(list[dict[str, str]], item.get("rejected", []))),
        }
        for item in cast(list[dict[str, object]], expected.get("decisions", []))
    )
    passed = (
        expected.get("outcome") == "assessment"
        and expected.get("disposition") == actual.disposition.value
        and tuple(cast(list[str], expected.get("missing", []))) == missing
        and tuple(cast(list[str], expected.get("conflicts", []))) == conflicts
        and tuple(sorted(cast(dict[str, str], expected.get("unavailable", {})).items()))
        == unavailable
        and decisions == expected_decisions
        and not actual.authorizes_external_action
        and not actual.closes_case
    )
    return CaseResult(
        case_id,
        passed,
        "assessment",
        actual.disposition.value,
        missing=missing,
        conflicts=conflicts,
        unavailable=unavailable,
    )


def evaluate_case(case_id: str) -> CaseResult:
    inputs = catalogue()[case_id]
    expected = _expected()[case_id]
    proposal_input, raw_observations = inputs
    try:
        proposal = (
            proposal_input
            if isinstance(proposal_input, InvestigationProposal)
            else InvestigationProposal.model_validate(proposal_input)
        )
        observations = raw_observations
        actual = assess_investigation(
            SCOPE,
            proposal,
            observations,
            REGISTRY,
            FreshnessPolicy.model_validate(POLICY),
            now=NOW,
        )
    except ContractViolation as exc:
        code = exc.code.value
        return CaseResult(
            case_id,
            expected.get("outcome") == "error" and expected.get("error_code") == code,
            "error",
            error_code=code,
        )
    except ValidationError:
        code = ErrorCode.INVALID_INPUT.value
        return CaseResult(
            case_id,
            expected.get("outcome") == "error" and expected.get("error_code") == code,
            "error",
            error_code=code,
        )
    return _assessment_result(case_id, actual, expected)


def evaluate_all() -> tuple[CaseResult, ...]:
    return tuple(evaluate_case(case_id) for case_id in sorted(catalogue()))


def result_digest(results: tuple[CaseResult, ...] | None = None) -> str:
    values = results if results is not None else evaluate_all()
    expectations = (ROOT / "expectations" / "v1.json").read_bytes()
    cases = "\n".join(sorted(catalogue())).encode("utf-8")
    payload = json.dumps(
        {
            "evaluator": EVALUATOR_VERSION,
            "cases": cases.decode(),
            "expectations_sha256": hashlib.sha256(expectations).hexdigest(),
            "results": [result.__dict__ for result in values],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    results = evaluate_all()
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        detail = result.disposition or result.error_code or ""
        print(f"{status} {result.case_id}: {detail}")
    print(f"digest={result_digest(results)}")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
