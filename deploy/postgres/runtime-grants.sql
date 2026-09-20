\set ON_ERROR_STOP on

-- Run after the first `aftercare-migrate` as the database owner.  The caller
-- supplies psql variables `database_name`, `migration_role`, and
-- `runtime_role`.  Use a dedicated Aftercare database: revoking PUBLIC schema
-- creation is intentionally database-wide.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO :"runtime_role";
GRANT USAGE ON SCHEMA public TO :"runtime_role";
ALTER ROLE :"runtime_role" NOSUPERUSER NOBYPASSRLS;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"runtime_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"runtime_role";

ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"runtime_role";

-- Runtime processes may read version ledgers for readiness, but may not forge
-- an applied migration or EGM schema version.
REVOKE INSERT, UPDATE, DELETE ON aftercare_schema_migrations FROM :"runtime_role";
REVOKE INSERT, UPDATE, DELETE ON egm_schema_version FROM :"runtime_role";
