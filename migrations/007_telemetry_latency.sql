-- Telemetry latency instrumentation: TTFT + prompt-cache hits.
--
-- TTFT separates queue wait from generation; cached_tokens separates cache
-- hits from unreported (NULL = unknown, never 0). Nullable + idempotent.
ALTER TABLE telemetry
    ADD COLUMN IF NOT EXISTS ttft_ms INT,
    ADD COLUMN IF NOT EXISTS cached_tokens INT;

CREATE INDEX IF NOT EXISTS idx_telemetry_ttft
    ON telemetry (model_used, ttft_ms);
