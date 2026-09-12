ALTER TABLE aftercare_inbox ADD COLUMN IF NOT EXISTS run_id text;
ALTER TABLE aftercare_inbox ADD COLUMN IF NOT EXISTS wait_id text;
ALTER TABLE aftercare_inbox ADD COLUMN IF NOT EXISTS generation bigint;
ALTER TABLE aftercare_inbox ADD COLUMN IF NOT EXISTS kind text;
ALTER TABLE aftercare_inbox ADD COLUMN IF NOT EXISTS correlation_key text;
ALTER TABLE aftercare_inbox ADD COLUMN IF NOT EXISTS condition_version text;

CREATE TABLE IF NOT EXISTS aftercare_waits (
    tenant_id text NOT NULL, case_id text NOT NULL, run_id text NOT NULL,
    wait_id text NOT NULL, generation bigint NOT NULL CHECK (generation > 0),
    kind text NOT NULL CHECK (kind IN ('input','approval')),
    correlation_key text NOT NULL, condition_version text NOT NULL,
    created_at timestamptz NOT NULL, deadline timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('PENDING','ACTIVE','SATISFIED','TIMED_OUT','CANCELLED')),
    resolved_by_event_id text,
    PRIMARY KEY (tenant_id, case_id, run_id, wait_id, generation),
    FOREIGN KEY (tenant_id, case_id, run_id) REFERENCES aftercare_runs(tenant_id, case_id, run_id),
    CHECK (deadline > created_at),
    CHECK ((state = 'SATISFIED') = (resolved_by_event_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS aftercare_inbox_wait_match_idx ON aftercare_inbox
    (tenant_id, case_id, run_id, wait_id, generation, kind, correlation_key, condition_version,
     received_at, event_id);
CREATE TABLE IF NOT EXISTS aftercare_wait_wakeups (
    tenant_id text NOT NULL, case_id text NOT NULL, run_id text NOT NULL,
    wait_id text NOT NULL, generation bigint NOT NULL CHECK (generation > 0),
    winner_event_id text, outcome text NOT NULL CHECK (outcome IN ('reply','timeout')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, run_id, wait_id, generation),
    FOREIGN KEY (tenant_id, case_id, run_id, wait_id, generation)
      REFERENCES aftercare_waits(tenant_id, case_id, run_id, wait_id, generation),
    CHECK ((outcome = 'reply') = (winner_event_id IS NOT NULL))
);
