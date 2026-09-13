-- Database authorization is the authority for Case scope.  A token's
-- case_ids claim may narrow this row, but it never creates a grant by itself.
CREATE TABLE IF NOT EXISTS aftercare_case_grants (
    tenant_id text NOT NULL,
    subject_id text NOT NULL,
    case_id text NOT NULL,
    permissions text[] NOT NULL CHECK (cardinality(permissions) > 0),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    granted_by text NOT NULL,
    granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz,
    revoked_at timestamptz,
    revoked_by text,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, subject_id, case_id),
    FOREIGN KEY (tenant_id, case_id) REFERENCES aftercare_cases(tenant_id, case_id),
    CHECK (expires_at IS NULL OR expires_at > granted_at),
    CHECK (revoked_at IS NULL OR revoked_at >= granted_at),
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
);
CREATE INDEX IF NOT EXISTS aftercare_case_grants_active_idx
    ON aftercare_case_grants (tenant_id, subject_id, case_id, expires_at)
    WHERE revoked_at IS NULL;
