-- Canonical, case-scoped investigation observation ledger.  EGM is a
-- projection/recall aid; this table remains the trusted business snapshot
-- used for deterministic assessment and replay.
CREATE TABLE IF NOT EXISTS aftercare_investigation_observations (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    order_id text NOT NULL,
    evidence_id text NOT NULL,
    source_id text NOT NULL,
    source_event_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN (
        'order_snapshot', 'logistics_observation', 'buyer_statement'
    )),
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    revoked_at timestamptz,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, evidence_id),
    UNIQUE (tenant_id, case_id, source_id, source_event_id),
    FOREIGN KEY (tenant_id, case_id)
        REFERENCES aftercare_cases(tenant_id, case_id),
    CHECK (observed_at <= received_at),
    CHECK (revoked_at IS NULL OR revoked_at >= received_at)
);
CREATE INDEX IF NOT EXISTS aftercare_investigation_observations_case_idx
    ON aftercare_investigation_observations
    (tenant_id, case_id, observed_at, evidence_id);
