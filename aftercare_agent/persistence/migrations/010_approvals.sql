-- Durable approval gate for high-risk Actions.  The approval is bound to the
-- exact Action digest and is never inferred from a model/tool message.
ALTER TABLE aftercare_actions
    ADD COLUMN IF NOT EXISTS approval_required boolean NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS aftercare_approvals (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    approval_id text NOT NULL,
    action_id text NOT NULL,
    action_parameters_sha256 char(64) NOT NULL
        CHECK (action_parameters_sha256 ~ '^[0-9a-f]{64}$'),
    policy_version text NOT NULL,
    requested_by text NOT NULL,
    expires_at timestamptz NOT NULL,
    decision text NOT NULL
        CHECK (decision IN ('PENDING','APPROVED','REJECTED','EXPIRED','CANCELLED')),
    approver text,
    decision_idempotency_key text,
    decision_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    decided_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, approval_id),
    UNIQUE (tenant_id, action_id),
    UNIQUE (tenant_id, decision_idempotency_key),
    FOREIGN KEY (tenant_id, case_id) REFERENCES aftercare_cases(tenant_id, case_id),
    FOREIGN KEY (tenant_id, action_id) REFERENCES aftercare_actions(tenant_id, action_id)
        ON DELETE CASCADE,
    CHECK (expires_at > created_at),
    CHECK ((decision = 'PENDING') =
        (approver IS NULL AND decision_idempotency_key IS NULL AND decided_at IS NULL)),
    CHECK (decision <> 'PENDING' OR decision_reason IS NULL)
);

CREATE INDEX IF NOT EXISTS aftercare_approvals_case_idx
    ON aftercare_approvals (tenant_id, case_id, decision, expires_at, approval_id);
CREATE INDEX IF NOT EXISTS aftercare_approvals_expiry_idx
    ON aftercare_approvals (tenant_id, expires_at, approval_id) WHERE decision = 'PENDING';
