import re
import subprocess
import sys
from pathlib import Path

from evals.runner import evaluate_all, evaluate_case, result_digest


def test_all_synthetic_cases_match_v1_expectations() -> None:
    results = evaluate_all()
    assert len(results) == 12
    assert all(result.passed for result in results)


def test_evaluation_digest_is_stable() -> None:
    assert result_digest() == result_digest()


def test_evaluation_digest_is_stable_across_processes() -> None:
    root = Path(__file__).parents[2]
    output = subprocess.check_output([sys.executable, "-m", "evals.runner"], cwd=root, text=True)
    match = re.search(r"^digest=([0-9a-f]{64})$", output, flags=re.MULTILINE)
    assert match is not None
    assert match.group(1) == result_digest()


def test_prompt_injection_is_rejected_at_model_boundary() -> None:
    result = evaluate_case("prompt-injection")
    assert result.passed
    assert result.error_code == "invalid_input"
