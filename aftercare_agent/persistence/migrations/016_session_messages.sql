CREATE UNIQUE INDEX IF NOT EXISTS aftercare_sessions_scope_key
    ON aftercare_sessions(tenant_id, case_id, session_id);

CREATE TABLE IF NOT EXISTS aftercare_session_messages (
    tenant_id text NOT NULL, case_id text NOT NULL, session_id text NOT NULL,
    message_id text NOT NULL, message_seq bigint NOT NULL CHECK (message_seq > 0),
    role text NOT NULL CHECK (role IN ('user','assistant','tool','system')),
    message_ref text NOT NULL, message_sha256 char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, session_id, message_id),
    UNIQUE (tenant_id, session_id, message_seq),
    FOREIGN KEY (tenant_id, case_id, session_id)
        REFERENCES aftercare_sessions(tenant_id, case_id, session_id)
);
CREATE INDEX IF NOT EXISTS aftercare_session_messages_scope_order
    ON aftercare_session_messages(tenant_id, case_id, session_id, message_seq);
