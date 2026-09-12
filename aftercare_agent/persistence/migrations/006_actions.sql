-- Durable external business obligations.  An Action is independent from a
-- Run/Attempt so two Cases can safely reference the same obligation.
CREATE TABLE IF NOT EXISTS aftercare_actions (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    order_id text NOT NULL,
    action_id text NOT NULL,
    action_type text NOT NULL,
    business_key text NOT NULL,
    idempotency_key text NOT NULL,
    parameters_sha256 char(64) NOT NULL CHECK (parameters_sha256 ~ '^[0-9a-f]{64}$'),
    amount_minor numeric(38,0) NOT NULL CHECK (amount_minor > 0),
    currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    provider_idempotency_key text,
    state text NOT NULL CHECK (state IN ('RESERVED','REQUESTED','UNKNOWN','CONFIRMED','FAILED')),
    provider_reference text,
    result_sha256 char(64) CHECK (result_sha256 IS NULL OR result_sha256 ~ '^[0-9a-f]{64}$'),
    failure_code text,
    fencing_token bigint CHECK (fencing_token IS NULL OR fencing_token > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, action_id),
    UNIQUE (tenant_id, business_key),
    UNIQUE (tenant_id, idempotency_key),
    FOREIGN KEY (tenant_id, case_id) REFERENCES aftercare_cases(tenant_id, case_id),
    CHECK ((state = 'CONFIRMED') = (provider_reference IS NOT NULL AND result_sha256 IS NOT NULL)),
    CHECK (state <> 'FAILED' OR failure_code IS NOT NULL),
    CHECK (state NOT IN ('RESERVED','REQUESTED','UNKNOWN') OR failure_code IS NULL)
);

CREATE INDEX IF NOT EXISTS aftercare_actions_case_idx
    ON aftercare_actions (tenant_id, case_id, created_at, action_id);
CREATE INDEX IF NOT EXISTS aftercare_actions_order_idx
    ON aftercare_actions (tenant_id, order_id, state, action_id);
CREATE INDEX IF NOT EXISTS aftercare_actions_unknown_idx
    ON aftercare_actions (tenant_id, updated_at, action_id) WHERE state = 'UNKNOWN';
