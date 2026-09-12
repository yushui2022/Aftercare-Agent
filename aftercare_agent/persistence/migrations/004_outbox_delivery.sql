-- Delivery state is intentionally separate from event identity.  Appending an
-- event is part of the business transaction; claiming and acknowledging it
-- happen in short, independent publisher transactions.
ALTER TABLE aftercare_outbox
    ADD COLUMN IF NOT EXISTS delivery_state text NOT NULL DEFAULT 'PENDING',
    ADD COLUMN IF NOT EXISTS delivery_owner text,
    ADD COLUMN IF NOT EXISTS delivery_lease_until timestamptz,
    ADD COLUMN IF NOT EXISTS delivery_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ADD COLUMN IF NOT EXISTS last_error text,
    ADD COLUMN IF NOT EXISTS delivered_at timestamptz;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'aftercare_outbox_delivery_state_ck'
    ) THEN
        ALTER TABLE aftercare_outbox ADD CONSTRAINT aftercare_outbox_delivery_state_ck CHECK (
            (delivery_state = 'PENDING' AND delivery_owner IS NULL AND delivery_lease_until IS NULL
                AND delivered_at IS NULL)
            OR (delivery_state = 'CLAIMED' AND delivery_owner IS NOT NULL
                AND delivery_lease_until IS NOT NULL AND delivered_at IS NULL)
            OR (delivery_state = 'ACKED' AND delivery_owner IS NULL
                AND delivery_lease_until IS NULL AND delivered_at IS NOT NULL)
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'aftercare_outbox_delivery_attempts_ck'
    ) THEN
        ALTER TABLE aftercare_outbox ADD CONSTRAINT aftercare_outbox_delivery_attempts_ck
            CHECK (delivery_attempts >= 0);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS aftercare_outbox_delivery_claim_idx
    ON aftercare_outbox (tenant_id, next_attempt_at, recorded_at, event_id)
    WHERE delivery_state = 'PENDING';
CREATE INDEX IF NOT EXISTS aftercare_outbox_delivery_expired_idx
    ON aftercare_outbox (tenant_id, delivery_lease_until, recorded_at, event_id)
    WHERE delivery_state = 'CLAIMED';
