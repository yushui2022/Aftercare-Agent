"""Versioned, provider-neutral model strategy configuration."""

from collections.abc import Mapping

from pydantic import ValidationError

from aftercare_agent.domain.common import ContractModel, ContractViolation, ErrorCode, Identifier

from .transport import DEFAULT_MODEL

DEFAULT_STRATEGY_ID = "aftercare-investigation"
DEFAULT_POLICY_VERSION = "policy-v1"
DEFAULT_TOOL_SCHEMA_VERSION = "tools-v1"


class ModelStrategy(ContractModel):
    """The immutable strategy identity pinned into a Run checkpoint.

    ``model`` is the provider model identifier.  The remaining versions are
    semantic configuration identities: changing any of them must be an
    explicit rollout decision rather than an accidental environment change.
    """

    strategy_id: Identifier
    model: Identifier
    config_version: Identifier
    policy_version: Identifier
    tool_schema_version: Identifier


def resolve_model_strategy(env: Mapping[str, str]) -> ModelStrategy:
    """Resolve one validated strategy from deployment configuration."""
    model = env.get("AFTERCARE_MODEL", "").strip() or DEFAULT_MODEL
    strategy_id = env.get("AFTERCARE_MODEL_STRATEGY_ID", "").strip() or DEFAULT_STRATEGY_ID
    config_version = env.get("AFTERCARE_MODEL_CONFIG_VERSION", "").strip() or model
    policy_version = env.get("AFTERCARE_MODEL_POLICY_VERSION", "").strip() or DEFAULT_POLICY_VERSION
    tool_schema_version = (
        env.get("AFTERCARE_MODEL_TOOL_SCHEMA_VERSION", "").strip() or DEFAULT_TOOL_SCHEMA_VERSION
    )
    try:
        return ModelStrategy(
            strategy_id=strategy_id,
            model=model,
            config_version=config_version,
            policy_version=policy_version,
            tool_schema_version=tool_schema_version,
        )
    except ValidationError as exc:
        raise ContractViolation(
            ErrorCode.INVALID_INPUT, "invalid model strategy configuration"
        ) from exc
