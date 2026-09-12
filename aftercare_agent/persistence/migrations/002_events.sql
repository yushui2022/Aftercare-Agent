CREATE TABLE IF NOT EXISTS aftercare_outbox (
    tenant_id text NOT NULL, case_id text NOT NULL, event_id text NOT NULL,
    case_seq bigint NOT NULL CHECK (case_seq > 0), event_type text NOT NULL,
    payload jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, event_id), UNIQUE (tenant_id, case_id, case_seq)
);

CREATE TABLE IF NOT EXISTS aftercare_inbox (
    tenant_id text NOT NULL, case_id text NOT NULL, event_id text NOT NULL,
    source_id text NOT NULL, source_event_id text NOT NULL, payload jsonb NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, source_id, source_event_id),
    UNIQUE (tenant_id, case_id, event_id)
);

CREATE TABLE IF NOT EXISTS aftercare_inbox_applications (
    tenant_id text NOT NULL, case_id text NOT NULL, consumer_id text NOT NULL,
    event_id text NOT NULL, applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, consumer_id, event_id)
);
