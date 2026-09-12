-- Cross-instance execution admission.  Limits and reservations live in
-- PostgreSQL so a Worker process cannot bypass the global/tenant budget with
-- a private semaphore.  A reservation is one current execution slice for a
-- Run; an expired reservation is reclaimable by the next owner.
CREATE TABLE IF NOT EXISTS aftercare_admission_limits (
    scope_kind text NOT NULL CHECK (scope_kind IN ('global', 'tenant')),
    scope_id text NOT NULL,
    max_active_slots bigint NOT NULL CHECK (max_active_slots > 0),
    PRIMARY KEY (scope_kind, scope_id),
    CHECK ((scope_kind = 'global' AND scope_id = 'global')
        OR (scope_kind = 'tenant' AND scope_id <> 'global'))
);

CREATE TABLE IF NOT EXISTS aftercare_execution_slots (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    run_id text NOT NULL,
    owner text NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    lease_until timestamptz NOT NULL,
    acquired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, case_id, run_id)
        REFERENCES aftercare_runs(tenant_id, case_id, run_id)
);
CREATE INDEX IF NOT EXISTS aftercare_execution_slots_expiry_idx
    ON aftercare_execution_slots (lease_until, tenant_id);

-- Queue rows are durable wake-ups.  The Run lease/fence remains the execution
-- authority; queue claim fields only prevent two schedulers selecting a row.
CREATE SEQUENCE IF NOT EXISTS aftercare_execution_queue_seq;
CREATE TABLE IF NOT EXISTS aftercare_execution_queue (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    run_id text NOT NULL,
    queue_seq bigint NOT NULL DEFAULT nextval('aftercare_execution_queue_seq'),
    priority integer NOT NULL DEFAULT 0,
    enqueued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL DEFAULT 'READY' CHECK (state IN ('READY', 'IN_FLIGHT')),
    claim_owner text,
    claim_token bigint,
    claimed_at timestamptz,
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, case_id, run_id)
        REFERENCES aftercare_runs(tenant_id, case_id, run_id),
    CHECK ((state = 'IN_FLIGHT') = (claim_owner IS NOT NULL AND claim_token IS NOT NULL
        AND claimed_at IS NOT NULL)),
    CHECK (state <> 'READY' OR (claim_owner IS NULL AND claim_token IS NULL AND claimed_at IS NULL))
);
CREATE INDEX IF NOT EXISTS aftercare_execution_queue_ready_idx
    ON aftercare_execution_queue (state, available_at, priority DESC, queue_seq);

-- A durable cursor gives basic tenant fairness across Worker processes.  The
-- cursor is advisory ordering, not an execution lock; the global limit row and
-- Run fence are still authoritative.
CREATE SEQUENCE IF NOT EXISTS aftercare_admission_dispatch_seq;
CREATE TABLE IF NOT EXISTS aftercare_tenant_fairness (
    tenant_id text PRIMARY KEY,
    last_dispatch_seq bigint NOT NULL DEFAULT 0 CHECK (last_dispatch_seq >= 0),
    dispatch_count bigint NOT NULL DEFAULT 0 CHECK (dispatch_count >= 0)
);

-- Retry budget is per Run.  Reservations are idempotent by retry_key, so a
-- producer retry cannot consume the same budget twice.
CREATE TABLE IF NOT EXISTS aftercare_retry_budgets (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    max_retries bigint NOT NULL CHECK (max_retries >= 0),
    retries_used bigint NOT NULL DEFAULT 0 CHECK (retries_used >= 0),
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES aftercare_runs(tenant_id, run_id),
    CHECK (retries_used <= max_retries)
);
CREATE TABLE IF NOT EXISTS aftercare_retry_reservations (
    tenant_id text NOT NULL,
    run_id text NOT NULL,
    retry_key text NOT NULL,
    units bigint NOT NULL CHECK (units > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, run_id, retry_key),
    FOREIGN KEY (tenant_id, run_id) REFERENCES aftercare_runs(tenant_id, run_id)
);
