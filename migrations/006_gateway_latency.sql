-- Latency instrumentation: TTFT + prompt-cache hits (gateway_usage ledger).
--
-- TTFT (time to first forwarded delta) separates free-tier queue tail from
-- total generation time; cached_tokens separates "cache miss" from "provider
-- did not report" (NULL = unknown, never 0). Both nullable so old rows stay
-- valid. Idempotent: re-runnable via IF NOT EXISTS / DO block.
DO $$
BEGIN
    ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS ttft_ms INT;
    ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS cached_tokens INT;
END $$;

CREATE INDEX IF NOT EXISTS idx_gateway_usage_ttft
    ON gateway_usage (provider, model, ttft_ms);
