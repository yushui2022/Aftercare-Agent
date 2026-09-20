"""The deployment seam: coordinates are explicit and a missing one fails closed."""

from importlib.resources import files
from pathlib import Path

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.model_adapters import resolve_model_strategy
from aftercare_agent.runtime.sandbox_executor import StaticCaseBinding
from aftercare_agent.runtime.wiring import build_model_harness, build_sandbox_executor

SAMPLE = Path(str(files("aftercare_agent.connectors").joinpath("data/commerce-sample.json")))
BINDING = StaticCaseBinding(order_id="A-1001")


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "AFTERCARE_COMMERCE_DATASET": str(SAMPLE),
        "AFTERCARE_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "AFTERCARE_SOURCE_ORDER_LEDGER": "erp-api",
        "AFTERCARE_SOURCE_CARRIER": "carrier-api",
        "AFTERCARE_SOURCE_BUYER_CHANNEL": "in-app",
    }


def test_the_sandbox_executor_needs_an_explicit_dataset_and_root(tmp_path: Path) -> None:
    with pytest.raises(ContractViolation) as error:
        build_sandbox_executor({}, owner="worker-a", case_binding=BINDING)
    assert error.value.code is ErrorCode.INVALID_INPUT

    env = _env(tmp_path)
    del env["AFTERCARE_SOURCE_CARRIER"]
    with pytest.raises(ContractViolation) as error:
        build_sandbox_executor(env, owner="worker-a", case_binding=BINDING)
    assert error.value.code is ErrorCode.INVALID_INPUT


def test_a_missing_provider_key_is_refused_rather_than_defaulted(tmp_path: Path) -> None:
    env = _env(tmp_path)
    executor = build_sandbox_executor(env, owner="worker-a", case_binding=BINDING)
    with pytest.raises(ValueError):
        build_model_harness(env, executor=executor, case_binding=BINDING, store=executor.store)


def test_pricing_beyond_the_free_tier_must_be_declared(tmp_path: Path) -> None:
    env = {**_env(tmp_path), "AFTERCARE_MODEL_API_KEY": "unit-test-key"}
    executor = build_sandbox_executor(env, owner="worker-a", case_binding=BINDING)
    with pytest.raises(ContractViolation) as error:
        build_model_harness(env, executor=executor, case_binding=BINDING, store=executor.store)
    assert error.value.code is ErrorCode.INVALID_INPUT

    priced = {**env, "AFTERCARE_MODEL_INPUT_MICROUSD_PER_TOKEN": "1"}
    priced["AFTERCARE_MODEL_OUTPUT_MICROUSD_PER_TOKEN"] = "2"
    harness = build_model_harness(
        priced, executor=executor, case_binding=BINDING, store=executor.store
    )
    assert callable(harness)


def test_model_strategy_is_explicitly_versioned() -> None:
    strategy = resolve_model_strategy(
        {
            "AFTERCARE_MODEL": "provider/model-a",
            "AFTERCARE_MODEL_CONFIG_VERSION": "model-a-config-2",
            "AFTERCARE_MODEL_POLICY_VERSION": "policy-3",
        }
    )
    assert strategy.model == "provider/model-a"
    assert strategy.config_version == "model-a-config-2"
    assert strategy.policy_version == "policy-3"
    assert strategy.tool_schema_version == "tools-v1"
