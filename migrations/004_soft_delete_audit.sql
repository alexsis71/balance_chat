BEGIN;

ALTER TABLE chat_rag.context_sessions_v2
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_context_sessions_v2_active
    ON chat_rag.context_sessions_v2 (updated_at DESC)
    WHERE deleted_at IS NULL;

REVOKE DELETE ON chat_rag.context_sessions_v2 FROM ai_agent;
REVOKE UPDATE, DELETE ON chat_rag.context_mutations_v2 FROM ai_agent;

COMMIT;
