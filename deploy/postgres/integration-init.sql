\set ON_ERROR_STOP on

-- Local integration credential only. The migration role is created by the
-- official PostgreSQL entrypoint from POSTGRES_USER; this script creates the
-- lower-privilege identity used by API and smoke services.
CREATE ROLE aftercare_runtime LOGIN PASSWORD 'integration-only-runtime';
