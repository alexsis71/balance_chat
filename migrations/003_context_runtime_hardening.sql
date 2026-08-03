BEGIN;

CREATE TABLE IF NOT EXISTS chat_rag.context_turn_reservations_v2 (
    session_id uuid PRIMARY KEY
        REFERENCES chat_rag.context_sessions_v2(session_id) ON DELETE CASCADE,
    request_id text NOT NULL UNIQUE,
    expected_revision bigint NOT NULL CHECK (expected_revision >= 0),
    expires_at timestamptz NOT NULL DEFAULT now() + interval '10 minutes'
);

CREATE TABLE IF NOT EXISTS chat_rag.context_request_results_v2 (
    session_id uuid NOT NULL
        REFERENCES chat_rag.context_sessions_v2(session_id) ON DELETE CASCADE,
    request_id text NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, request_id)
);

CREATE TABLE IF NOT EXISTS chat_rag.result_memory_outbox_v2 (
    session_id uuid NOT NULL
        REFERENCES chat_rag.context_sessions_v2(session_id) ON DELETE CASCADE,
    revision bigint NOT NULL CHECK (revision > 0),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz,
    PRIMARY KEY (session_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_result_memory_outbox_pending_v2
    ON chat_rag.result_memory_outbox_v2 (created_at)
    WHERE delivered_at IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON chat_rag.context_turn_reservations_v2 TO ai_agent;
GRANT SELECT, INSERT
    ON chat_rag.context_request_results_v2 TO ai_agent;
GRANT SELECT, INSERT, UPDATE
    ON chat_rag.result_memory_outbox_v2 TO ai_agent;

COMMIT;
