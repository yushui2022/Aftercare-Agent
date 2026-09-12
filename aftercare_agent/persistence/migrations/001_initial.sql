CREATE TABLE IF NOT EXISTS aftercare_admissions (
    tenant_id text NOT NULL, entrypoint text NOT NULL, idempotency_key text NOT NULL,
    payload_digest char(64) NOT NULL, case_id text NOT NULL, session_id text NOT NULL,
    run_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, entrypoint, idempotency_key)
);

CREATE TABLE IF NOT EXISTS aftercare_cases (
    tenant_id text NOT NULL, case_id text NOT NULL, order_id text NOT NULL,
    version bigint NOT NULL CHECK (version > 0), status text NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'IN_REVIEW', 'CLOSED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id)
);
CREATE TABLE IF NOT EXISTS aftercare_sessions (
    tenant_id text NOT NULL, case_id text NOT NULL, session_id text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('buyer', 'support', 'synthetic')),
    PRIMARY KEY (tenant_id, session_id), FOREIGN KEY (tenant_id, case_id)
        REFERENCES aftercare_cases(tenant_id, case_id)
);
CREATE TABLE IF NOT EXISTS aftercare_runs (
    tenant_id text NOT NULL, case_id text NOT NULL, run_id text NOT NULL,
    session_id text, predecessor_run_id text, definition_version text NOT NULL,
    input_version bigint NOT NULL CHECK (input_version > 0), state text NOT NULL
        CHECK (state IN ('READY','RUNNING','WAITING_INPUT','WAITING_APPROVAL','RETRY_AT','REVIEW','COMPLETED','CANCELLED')),
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0), lease_owner text,
    lease_until timestamptz, wait_id text, wait_generation bigint, available_at timestamptz,
    PRIMARY KEY (tenant_id, run_id), UNIQUE (tenant_id, case_id, run_id),
    FOREIGN KEY (tenant_id, case_id) REFERENCES aftercare_cases(tenant_id, case_id),
    CHECK ((state = 'RUNNING') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL AND fencing_token > 0)),
    CHECK ((state IN ('WAITING_INPUT','WAITING_APPROVAL')) = (wait_id IS NOT NULL AND wait_generation IS NOT NULL)),
    CHECK ((state = 'RETRY_AT') = (available_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS aftercare_steps (
    tenant_id text NOT NULL, case_id text NOT NULL, run_id text NOT NULL, step_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('model','tool','evaluate')), input_version bigint NOT NULL CHECK (input_version > 0),
    PRIMARY KEY (tenant_id, run_id, step_id), UNIQUE (tenant_id, case_id, run_id, step_id),
    FOREIGN KEY (tenant_id, case_id, run_id)
        REFERENCES aftercare_runs(tenant_id, case_id, run_id)
);
CREATE TABLE IF NOT EXISTS aftercare_attempts (
    tenant_id text NOT NULL, case_id text NOT NULL, run_id text NOT NULL, step_id text NOT NULL,
    attempt_id text NOT NULL, attempt_number bigint NOT NULL CHECK (attempt_number > 0),
    fencing_token bigint NOT NULL CHECK (fencing_token > 0), status text NOT NULL
        CHECK (status IN ('STARTED','SUCCEEDED','FAILED','UNKNOWN')),
    PRIMARY KEY (tenant_id, run_id, attempt_id), FOREIGN KEY (tenant_id, case_id, run_id, step_id)
        REFERENCES aftercare_steps(tenant_id, case_id, run_id, step_id)
);
CREATE TABLE IF NOT EXISTS aftercare_checkpoints (
    tenant_id text NOT NULL, case_id text NOT NULL, run_id text NOT NULL,
    checkpoint_version bigint NOT NULL CHECK (checkpoint_version > 0),
    saved_fencing_token bigint NOT NULL CHECK (saved_fencing_token > 0), payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, checkpoint_version), FOREIGN KEY (tenant_id, case_id, run_id)
        REFERENCES aftercare_runs(tenant_id, case_id, run_id)
);
