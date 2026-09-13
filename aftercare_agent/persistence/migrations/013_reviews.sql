-- Durable human-review gate for Runs that cannot safely continue automatically.
-- A review request is immutable except for its single terminal decision.
CREATE TABLE IF NOT EXISTS aftercare_reviews (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    run_id text NOT NULL,
    review_id text NOT NULL,
    reason_code text NOT NULL,
    evidence_sha256 char(64) NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    policy_version text NOT NULL,
    requested_by text NOT NULL,
    input_version bigint NOT NULL CHECK (input_version > 0),
    decision text CHECK (decision IN ('CONTINUE','CANCEL')),
    reviewer text,
    decision_idempotency_key text,
    decision_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    decided_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, review_id),
    FOREIGN KEY (tenant_id, case_id, run_id)
        REFERENCES aftercare_runs(tenant_id, case_id, run_id),
    CHECK (
        (decision IS NULL AND reviewer IS NULL AND decision_idempotency_key IS NULL AND decided_at IS NULL)
        OR
        (decision IS NOT NULL AND reviewer IS NOT NULL AND decision_idempotency_key IS NOT NULL AND decided_at IS NOT NULL)
    ),
    CHECK (decision IS NOT NULL OR decision_reason IS NULL),
    UNIQUE (tenant_id, decision_idempotency_key)
);

-- A Run may have at most one outstanding review request.  Once resolved, a
-- later REVIEW state may create a new request with a new review_id.
CREATE UNIQUE INDEX IF NOT EXISTS aftercare_reviews_pending_run_idx
    ON aftercare_reviews (tenant_id, run_id)
    WHERE decision IS NULL;

CREATE INDEX IF NOT EXISTS aftercare_reviews_case_idx
    ON aftercare_reviews (tenant_id, case_id, created_at DESC, review_id DESC);
