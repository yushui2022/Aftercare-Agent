-- Durable receipt for projecting one Aftercare observation into EGM.
--
-- The expected revision is part of EGM's idempotency payload.  Persisting it
-- before the EGM call lets a Worker replay the exact same operation when it
-- crashes after EGM commits but before Aftercare records completion.
CREATE TABLE IF NOT EXISTS aftercare_investigation_egm_projections (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    order_id text NOT NULL,
    evidence_id text NOT NULL,
    operation_id text NOT NULL CHECK (operation_id <> ''),
    expected_revision bigint NOT NULL CHECK (expected_revision >= 0),
    completed_revision bigint,
    egm_evidence_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, evidence_id),
    UNIQUE (tenant_id, case_id, operation_id),
    FOREIGN KEY (tenant_id, case_id, evidence_id)
        REFERENCES aftercare_investigation_observations(tenant_id, case_id, evidence_id),
    CHECK ((completed_revision IS NULL) = (egm_evidence_id IS NULL)),
    CHECK (completed_revision IS NULL OR completed_revision > expected_revision)
);
