BEGIN;

CREATE SCHEMA IF NOT EXISTS chat_rag;

CREATE TABLE IF NOT EXISTS chat_rag.context_sessions_v2 (
    session_id uuid PRIMARY KEY,
    contract_version text NOT NULL CHECK (contract_version = '2.0'),
    revision bigint NOT NULL CHECK (revision >= 0),
    metadata_bundle_id text,
    metadata_bundle_version text,
    metadata_schema_version text,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_rag.context_mutations_v2 (
    session_id uuid NOT NULL
        REFERENCES chat_rag.context_sessions_v2(session_id) ON DELETE CASCADE,
    revision bigint NOT NULL CHECK (revision > 0),
    turn_id uuid NOT NULL,
    outcome text NOT NULL
        CHECK (outcome IN ('success', 'no_data', 'error', 'clarification')),
    mutation jsonb NOT NULL,
    contract_sha256 text NOT NULL
        CHECK (contract_sha256 ~ '^sha256:[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, revision),
    UNIQUE (session_id, turn_id)
);

CREATE INDEX IF NOT EXISTS idx_context_mutations_v2_turn
    ON chat_rag.context_mutations_v2 (session_id, created_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON chat_rag.context_sessions_v2 TO ai_agent;
GRANT SELECT, INSERT, DELETE
    ON chat_rag.context_mutations_v2 TO ai_agent;

COMMIT;
