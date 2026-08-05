#!/usr/bin/env bash
# Verify the gateway audit trail (gateway_usage ledger + telemetry rows).
set -a
source .env
set +a
docker exec bucker-pg psql -U postgres -d bucker -c \
  "SELECT request_id::text, provider, model, prompt_tokens, completion_tokens,
          round(cost_usd::numeric, 8) AS cost_usd, outcome, attempt_count,
          to_char(created_at, 'HH24:MI:SS') AS at
   FROM gateway_usage ORDER BY id DESC LIMIT 5;"
docker exec bucker-pg psql -U postgres -d bucker -c \
  "SELECT model_used, purpose, prompt_tokens, completion_tokens,
          round(cost_usd::numeric, 8) AS cost_usd, to_char(created_at, 'HH24:MI:SS') AS at
   FROM telemetry WHERE purpose = 'gateway' ORDER BY event_id DESC LIMIT 5;"
