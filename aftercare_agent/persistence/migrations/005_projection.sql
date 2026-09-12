-- Case-local projection ledger.  A position is advanced only while its
-- corresponding applied event is inserted in the same transaction.
CREATE TABLE IF NOT EXISTS aftercare_projection_positions (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    consumer_id text NOT NULL,
    last_case_seq bigint NOT NULL DEFAULT 0 CHECK (last_case_seq >= 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, consumer_id)
);

-- The full event is retained so replay and same-sequence conflicts compare the
-- complete contract, not merely an event id or a hash.
CREATE TABLE IF NOT EXISTS aftercare_projection_applied (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    consumer_id text NOT NULL,
    case_seq bigint NOT NULL CHECK (case_seq > 0),
    event_id text NOT NULL,
    payload jsonb NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, consumer_id, event_id),
    UNIQUE (tenant_id, case_id, consumer_id, case_seq)
);
CREATE INDEX IF NOT EXISTS aftercare_projection_applied_seq_idx
    ON aftercare_projection_applied (tenant_id, case_id, consumer_id, case_seq);

-- Future events are retained until all preceding case-local sequences arrive.
CREATE TABLE IF NOT EXISTS aftercare_projection_buffer (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    consumer_id text NOT NULL,
    case_seq bigint NOT NULL CHECK (case_seq > 0),
    event_id text NOT NULL,
    payload jsonb NOT NULL,
    buffered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, consumer_id, event_id),
    UNIQUE (tenant_id, case_id, consumer_id, case_seq)
);
CREATE INDEX IF NOT EXISTS aftercare_projection_buffer_seq_idx
    ON aftercare_projection_buffer (tenant_id, case_id, consumer_id, case_seq);
