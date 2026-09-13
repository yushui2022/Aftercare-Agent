-- Bind an approval to a durable approval wait when the orchestration layer
-- needs a decision to wake a WAITING_APPROVAL Run atomically.
ALTER TABLE aftercare_approvals
    ADD COLUMN IF NOT EXISTS run_id text,
    ADD COLUMN IF NOT EXISTS wait_id text,
    ADD COLUMN IF NOT EXISTS wait_generation bigint;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'aftercare_approvals_run_fk'
    ) THEN
        ALTER TABLE aftercare_approvals
            ADD CONSTRAINT aftercare_approvals_run_fk
            FOREIGN KEY (tenant_id, case_id, run_id)
            REFERENCES aftercare_runs(tenant_id, case_id, run_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'aftercare_approvals_wait_fk'
    ) THEN
        ALTER TABLE aftercare_approvals
            ADD CONSTRAINT aftercare_approvals_wait_fk
            FOREIGN KEY (tenant_id, case_id, run_id, wait_id, wait_generation)
            REFERENCES aftercare_waits(tenant_id, case_id, run_id, wait_id, generation);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'aftercare_approvals_wait_binding_check'
    ) THEN
        ALTER TABLE aftercare_approvals
            ADD CONSTRAINT aftercare_approvals_wait_binding_check
            CHECK (
                (run_id IS NULL AND wait_id IS NULL AND wait_generation IS NULL)
                OR (run_id IS NOT NULL AND wait_id IS NOT NULL AND wait_generation IS NOT NULL
                    AND wait_generation > 0)
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS aftercare_approvals_wait_idx
    ON aftercare_approvals (tenant_id, case_id, run_id, wait_id, wait_generation)
    WHERE run_id IS NOT NULL;
