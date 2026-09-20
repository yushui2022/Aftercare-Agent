\set ON_ERROR_STOP on

BEGIN;
SELECT set_config('aftercare.tenant_id', 'rls-tenant-a', true);
DO $$
DECLARE
    visible_count integer;
BEGIN
    SELECT count(*) INTO visible_count
    FROM aftercare_cases
    WHERE case_id IN ('rls-case-a', 'rls-case-b');
    IF visible_count <> 1 THEN
        RAISE EXCEPTION 'tenant A saw % cases, expected 1', visible_count;
    END IF;
END
$$;

SELECT set_config('aftercare.tenant_id', 'rls-tenant-b', true);
DO $$
DECLARE
    visible_count integer;
    inserted boolean := false;
BEGIN
    SELECT count(*) INTO visible_count
    FROM aftercare_cases
    WHERE case_id IN ('rls-case-a', 'rls-case-b');
    IF visible_count <> 1 THEN
        RAISE EXCEPTION 'tenant B saw % cases, expected 1', visible_count;
    END IF;

    BEGIN
        INSERT INTO aftercare_cases(tenant_id, case_id, order_id, version, status)
        VALUES ('rls-tenant-a', 'rls-cross-tenant-write', 'rls-cross-tenant-order', 1, 'OPEN');
        inserted := true;
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    IF inserted THEN
        RAISE EXCEPTION 'tenant B inserted a tenant A row';
    END IF;
END
$$;
ROLLBACK;
