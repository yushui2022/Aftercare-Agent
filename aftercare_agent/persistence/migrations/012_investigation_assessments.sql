-- Immutable, case/run-scoped assessment snapshots for restart and audit.
CREATE TABLE IF NOT EXISTS aftercare_investigation_assessments (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    run_id text NOT NULL,
    assessment_id text NOT NULL,
    assessment_sha256 char(64) NOT NULL,
    policy_id text NOT NULL,
    policy_version integer NOT NULL CHECK (policy_version >= 1),
    disposition text NOT NULL CHECK (disposition IN ('needs_material', 'human_review', 'recommendation_ready')),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id, assessment_id),
    FOREIGN KEY (tenant_id, case_id, run_id)
        REFERENCES aftercare_runs(tenant_id, case_id, run_id)
);
CREATE INDEX IF NOT EXISTS aftercare_investigation_assessments_latest_idx
    ON aftercare_investigation_assessments (tenant_id, case_id, run_id, created_at DESC, assessment_id DESC);
