-- Preserve the source correction and the exact EGM command across crashes.
ALTER TABLE aftercare_investigation_observations
    ADD COLUMN IF NOT EXISTS revocation_reason text;

UPDATE aftercare_investigation_observations
SET revocation_reason = 'legacy revocation'
WHERE revoked_at IS NOT NULL AND revocation_reason IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'aftercare_investigation_observations_revocation_reason_check'
          AND conrelid = 'aftercare_investigation_observations'::regclass
    ) THEN
        ALTER TABLE aftercare_investigation_observations
            ADD CONSTRAINT aftercare_investigation_observations_revocation_reason_check
            CHECK (
                (revoked_at IS NULL) = (revocation_reason IS NULL)
                AND (revocation_reason IS NULL OR (
                    length(btrim(revocation_reason)) > 0
                    AND length(revocation_reason) <= 4000
                ))
            );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS aftercare_investigation_egm_revocations (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    order_id text NOT NULL,
    evidence_id text NOT NULL,
    egm_evidence_id text NOT NULL CHECK (egm_evidence_id <> ''),
    operation_id text NOT NULL CHECK (operation_id <> ''),
    reason text NOT NULL CHECK (length(btrim(reason)) > 0 AND length(reason) <= 4000),
    revoked_at timestamptz NOT NULL,
    expected_revision bigint NOT NULL CHECK (expected_revision >= 0),
    completed_revision bigint,
    egm_revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, evidence_id),
    UNIQUE (tenant_id, case_id, operation_id),
    FOREIGN KEY (tenant_id, case_id, evidence_id)
        REFERENCES aftercare_investigation_egm_projections(tenant_id, case_id, evidence_id),
    CHECK ((completed_revision IS NULL) = (egm_revoked_at IS NULL)),
    CHECK (completed_revision IS NULL OR completed_revision > expected_revision)
);
