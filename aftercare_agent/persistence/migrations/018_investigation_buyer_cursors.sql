-- Durable position for trusted host ingestion of buyer-message history.
-- The observation ledger remains authoritative; advancing this cursor only
-- says every source event through message_id was projected successfully.
CREATE TABLE IF NOT EXISTS aftercare_investigation_buyer_cursors (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    order_id text NOT NULL,
    source_id text NOT NULL,
    message_id text NOT NULL CHECK (message_id <> ''),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, source_id),
    FOREIGN KEY (tenant_id, case_id)
        REFERENCES aftercare_cases(tenant_id, case_id)
);
