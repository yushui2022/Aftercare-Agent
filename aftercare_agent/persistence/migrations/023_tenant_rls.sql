-- Enable the second tenant-isolation boundary for every Aftercare table that
-- carries tenant_id.  The migration/owner role remains the schema owner and
-- therefore can apply future migrations; runtime roles are configured with
-- NOBYPASSRLS in the deployment grant step.
DO $$
DECLARE
    table_name text;
BEGIN
    FOR table_name IN
        SELECT DISTINCT c.table_name
        FROM information_schema.columns AS c
        WHERE c.table_schema = 'public'
          AND c.column_name = 'tenant_id'
          AND c.table_name LIKE 'aftercare_%'
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS aftercare_tenant_context ON %I', table_name);
        EXECUTE format(
            'CREATE POLICY aftercare_tenant_context ON %I '
            'USING (tenant_id = NULLIF(current_setting(''aftercare.tenant_id'', true), '''')) '
            'WITH CHECK (tenant_id = NULLIF(current_setting(''aftercare.tenant_id'', true), ''''))',
            table_name
        );
    END LOOP;
END
$$;
