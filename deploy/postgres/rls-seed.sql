\set ON_ERROR_STOP on

BEGIN;
INSERT INTO aftercare_cases(tenant_id, case_id, order_id, version, status)
VALUES
    ('rls-tenant-a', 'rls-case-a', 'rls-order-a', 1, 'OPEN'),
    ('rls-tenant-b', 'rls-case-b', 'rls-order-b', 1, 'OPEN')
ON CONFLICT (tenant_id, case_id) DO NOTHING;
COMMIT;
