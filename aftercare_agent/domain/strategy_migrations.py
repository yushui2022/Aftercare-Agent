"""Contracts for explicit, audited model-strategy migration."""

from pydantic import Field

from .common import Identifier, PositiveInt, RunScope, UtcDatetime


class StrategyMigrationRequest(RunScope):
    """Change the strategy identity of a stopped Run after an operator review."""

    checkpoint_version: PositiveInt
    old_strategy_id: Identifier
    old_model_config_version: Identifier
    old_policy_version: Identifier
    old_tool_schema_version: Identifier
    new_strategy_id: Identifier
    new_model_config_version: Identifier
    new_policy_version: Identifier
    new_tool_schema_version: Identifier
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: Identifier


class StrategyMigrationRecord(RunScope):
    """Immutable audit record for one applied strategy migration."""

    migration_id: Identifier
    checkpoint_version_before: PositiveInt
    checkpoint_version_after: PositiveInt
    old_strategy_id: Identifier
    old_model_config_version: Identifier
    old_policy_version: Identifier
    old_tool_schema_version: Identifier
    new_strategy_id: Identifier
    new_model_config_version: Identifier
    new_policy_version: Identifier
    new_tool_schema_version: Identifier
    reason: str = Field(min_length=1, max_length=2000)
    migrated_by: Identifier
    idempotency_key: Identifier
    created_at: UtcDatetime


__all__ = ["StrategyMigrationRecord", "StrategyMigrationRequest"]
