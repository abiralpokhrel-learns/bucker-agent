-- Telemetry token columns (usage dashboard).
--
-- The router already captures prompt/completion/total tokens in every model
-- call (litellm usage); this makes them queryable in the DB so the dashboard
-- can answer "which API is burning how many tokens". Additive only, and
-- idempotent: `bucker migrate` re-runs every file, so this must be safe to
-- apply more than once.

ALTER TABLE telemetry
    ADD COLUMN IF NOT EXISTS purpose          TEXT,
    ADD COLUMN IF NOT EXISTS prompt_tokens    INT,
    ADD COLUMN IF NOT EXISTS completion_tokens INT,
    ADD COLUMN IF NOT EXISTS total_tokens     INT;

CREATE INDEX IF NOT EXISTS idx_telemetry_purpose
    ON telemetry (purpose, created_at);
