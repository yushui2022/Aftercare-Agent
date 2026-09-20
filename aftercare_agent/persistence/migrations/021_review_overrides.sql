-- Immutable, case-scoped audit records for operator-authorized Review recovery.
-- The checkpoint update and the Review decision are committed in the same
-- transaction by ReviewRepository; this table is the durable explanation of
-- why additional execution budget became available.
CREATE TABLE IF NOT EXISTS aftercare_review_overrides (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    run_id text NOT NULL,
    review_id text NOT NULL,
    override_id text NOT NULL,
    checkpoint_version bigint NOT NULL CHECK (checkpoint_version > 0),
    model_calls_add bigint NOT NULL CHECK (model_calls_add >= 0),
    tool_calls_add bigint NOT NULL CHECK (tool_calls_add >= 0),
    cost_microusd_add bigint NOT NULL CHECK (cost_microusd_add >= 0),
    deadline_extension_seconds bigint NOT NULL CHECK (deadline_extension_seconds >= 0),
    reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 2000),
    created_by text NOT NULL,
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, override_id),
    FOREIGN KEY (tenant_id, case_id, run_id)
        REFERENCES aftercare_runs(tenant_id, case_id, run_id),
    FOREIGN KEY (tenant_id, review_id)
        REFERENCES aftercare_reviews(tenant_id, review_id),
    CHECK (
        model_calls_add > 0 OR tool_calls_add > 0 OR cost_microusd_add > 0
        OR deadline_extension_seconds > 0
    ),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS aftercare_review_overrides_review_idx
    ON aftercare_review_overrides (tenant_id, review_id, created_at DESC, override_id DESC);
