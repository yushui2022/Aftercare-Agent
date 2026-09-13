-- Case-local event allocation and immutable gate-event snapshots.
-- The counter is intentionally independent of aftercare_cases so legacy
-- fixtures/imports that append events before creating a case remain valid.
CREATE TABLE IF NOT EXISTS aftercare_case_event_sequences (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    last_case_seq bigint NOT NULL CHECK (last_case_seq >= 0),
    PRIMARY KEY (tenant_id, case_id)
);

CREATE TABLE IF NOT EXISTS aftercare_event_payloads (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    reference_id text NOT NULL,
    sha256 char(64) NOT NULL,
    payload jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, reference_id),
    UNIQUE (tenant_id, case_id, reference_id, sha256)
);

-- Seed high-water marks for events written by versions before this migration.
INSERT INTO aftercare_case_event_sequences(tenant_id, case_id, last_case_seq)
SELECT tenant_id, case_id, MAX(case_seq)
FROM aftercare_outbox
GROUP BY tenant_id, case_id
ON CONFLICT (tenant_id, case_id) DO UPDATE
SET last_case_seq = GREATEST(
    aftercare_case_event_sequences.last_case_seq,
    EXCLUDED.last_case_seq
);
