-- Stable mapping between an Aftercare investigation Case and its EGM node.
-- EGM owns the node revision; this table only remembers the node identity and
-- the schema fingerprint used when it was created.
CREATE TABLE IF NOT EXISTS aftercare_investigation_egm_bindings (
    tenant_id text NOT NULL,
    case_id text NOT NULL,
    order_id text NOT NULL,
    node_id text NOT NULL,
    schema_fingerprint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, case_id),
    UNIQUE (node_id),
    FOREIGN KEY (tenant_id, case_id)
        REFERENCES aftercare_cases(tenant_id, case_id)
);
