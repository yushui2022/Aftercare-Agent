-- Keep the durable execution queue aligned with the authoritative Run state.
-- A queue row is only a wake-up/ordering record; the Run lease and fencing
-- token remain the execution authority.
ALTER TABLE aftercare_execution_queue
    DROP CONSTRAINT aftercare_execution_queue_tenant_id_case_id_run_id_fkey;
ALTER TABLE aftercare_execution_queue
    ADD CONSTRAINT aftercare_execution_queue_run_fkey
    FOREIGN KEY (tenant_id, case_id, run_id)
    REFERENCES aftercare_runs(tenant_id, case_id, run_id) ON DELETE CASCADE;

CREATE OR REPLACE FUNCTION aftercare_sync_execution_queue()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.state = 'RUNNING' THEN
        INSERT INTO aftercare_execution_queue(
            tenant_id, case_id, run_id, available_at, state,
            claim_owner, claim_token, claimed_at
        ) VALUES (
            NEW.tenant_id, NEW.case_id, NEW.run_id,
            clock_timestamp(), 'IN_FLIGHT',
            NEW.lease_owner, NEW.fencing_token, clock_timestamp()
        )
        ON CONFLICT (tenant_id, run_id) DO UPDATE SET
            case_id = EXCLUDED.case_id,
            state = 'IN_FLIGHT',
            claim_owner = EXCLUDED.claim_owner,
            claim_token = EXCLUDED.claim_token,
            claimed_at = EXCLUDED.claimed_at;
    ELSIF NEW.state IN ('READY', 'RETRY_AT') THEN
        INSERT INTO aftercare_execution_queue(
            tenant_id, case_id, run_id, available_at, state,
            claim_owner, claim_token, claimed_at
        ) VALUES (
            NEW.tenant_id, NEW.case_id, NEW.run_id,
            COALESCE(NEW.available_at, clock_timestamp()), 'READY',
            NULL, NULL, NULL
        )
        ON CONFLICT (tenant_id, run_id) DO UPDATE SET
            case_id = EXCLUDED.case_id,
            available_at = EXCLUDED.available_at,
            state = 'READY',
            claim_owner = NULL,
            claim_token = NULL,
            claimed_at = NULL;
    ELSE
        DELETE FROM aftercare_execution_queue
        WHERE tenant_id = NEW.tenant_id AND run_id = NEW.run_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS aftercare_runs_execution_queue_sync ON aftercare_runs;
CREATE TRIGGER aftercare_runs_execution_queue_sync
AFTER INSERT OR UPDATE OF state, available_at, lease_owner, fencing_token
ON aftercare_runs
FOR EACH ROW EXECUTE FUNCTION aftercare_sync_execution_queue();

-- A database may already contain Runs created after migration 007 but before
-- this trigger existed.  Backfill only runnable states and preserve an
-- operator-selected priority when a queue row is already present.
INSERT INTO aftercare_execution_queue(
    tenant_id, case_id, run_id, available_at, state,
    claim_owner, claim_token, claimed_at
)
SELECT tenant_id, case_id, run_id,
       COALESCE(available_at, clock_timestamp()), 'READY',
       NULL, NULL, NULL
FROM aftercare_runs
WHERE state IN ('READY', 'RETRY_AT')
ON CONFLICT (tenant_id, run_id) DO UPDATE SET
    case_id = EXCLUDED.case_id,
    available_at = EXCLUDED.available_at,
    state = 'READY',
    claim_owner = NULL,
    claim_token = NULL,
    claimed_at = NULL;

INSERT INTO aftercare_execution_queue(
    tenant_id, case_id, run_id, available_at, state,
    claim_owner, claim_token, claimed_at
)
SELECT tenant_id, case_id, run_id, clock_timestamp(), 'IN_FLIGHT',
       lease_owner, fencing_token, clock_timestamp()
FROM aftercare_runs
WHERE state = 'RUNNING'
ON CONFLICT (tenant_id, run_id) DO UPDATE SET
    case_id = EXCLUDED.case_id,
    state = 'IN_FLIGHT',
    claim_owner = EXCLUDED.claim_owner,
    claim_token = EXCLUDED.claim_token,
    claimed_at = EXCLUDED.claimed_at;

DELETE FROM aftercare_execution_queue q
USING aftercare_runs r
WHERE q.tenant_id=r.tenant_id AND q.run_id=r.run_id
    AND r.state NOT IN ('READY', 'RETRY_AT', 'RUNNING');
