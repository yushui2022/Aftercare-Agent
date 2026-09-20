-- Explicit, immutable model-strategy migrations.  A migration only changes
-- the checkpoint identity; a pending human Review still owns REVIEW -> READY.
CREATE TABLE IF NOT EXISTS aftercare_strategy_migrations (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    run_id text NOT NULL,
    migration_id text NOT NULL,
    checkpoint_version_before bigint NOT NULL CHECK (checkpoint_version_before > 0),
    checkpoint_version_after bigint NOT NULL CHECK (checkpoint_version_after > checkpoint_version_before),
    old_strategy_id text NOT NULL,
    old_model_config_version text NOT NULL,
    old_policy_version text NOT NULL,
    old_tool_schema_version text NOT NULL,
    new_strategy_id text NOT NULL,
    new_model_config_version text NOT NULL,
    new_policy_version text NOT NULL,
    new_tool_schema_version text NOT NULL,
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
    migrated_by text NOT NULL,
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, migration_id),
    FOREIGN KEY (tenant_id, case_id, run_id)
        REFERENCES aftercare_runs(tenant_id, case_id, run_id),
    CHECK (
        old_strategy_id <> new_strategy_id
        OR old_model_config_version <> new_model_config_version
        OR old_policy_version <> new_policy_version
        OR old_tool_schema_version <> new_tool_schema_version
    ),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS aftercare_strategy_migrations_run_idx
    ON aftercare_strategy_migrations (tenant_id, case_id, run_id, created_at DESC, migration_id DESC);
