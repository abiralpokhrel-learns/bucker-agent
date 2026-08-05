-- Gateway usage ledger (inference gateway quota/cost tracking).
--
-- One row per gateway inference request (success OR failure): the ledger is
-- the durable record behind quota enforcement ("how much of the daily free
-- entitlement is left for this provider/model") and the cost dashboard.
-- Additive only, and idempotent: `bucker migrate` re-runs every file, so
-- this must be safe to apply more than once.

CREATE TABLE IF NOT EXISTS gateway_usage (
    id                BIGSERIAL PRIMARY KEY,
    request_id        TEXT NOT NULL,
    tenant_id         TEXT NOT NULL DEFAULT 'default',
    purpose           TEXT,
    provider          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    prompt_tokens     INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens      INT NOT NULL DEFAULT 0,
    cost_usd          DOUBLE PRECISION,          -- NULL = unknown, never 0
    latency_ms        INT NOT NULL DEFAULT 0,
    outcome           TEXT NOT NULL,             -- success | error
    error_type        TEXT,
    attempt_count     INT NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Quota checks group by provider/model per day; request_id dedupes audits.
CREATE INDEX IF NOT EXISTS idx_gateway_usage_day
    ON gateway_usage (provider, model, created_at);
CREATE INDEX IF NOT EXISTS idx_gateway_usage_request
    ON gateway_usage (request_id);

-- The app role writes the ledger (INSERT) and the dashboard reads it
-- (SELECT), exactly like the telemetry table in 001_init.sql.
GRANT INSERT, SELECT ON gateway_usage TO bucker_app;
GRANT USAGE, SELECT ON SEQUENCE gateway_usage_id_seq TO bucker_app;
