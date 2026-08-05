#!/usr/bin/env bash
# E2E smoke: real chat completion + stream through the live gateway.
# Reads the token from .env without printing it.
set -a
source .env
set +a
TOKEN="$BUCKER_API_TOKEN"
echo "=== non-stream (priority: deepseek -> ollama -> openrouter free) ==="
curl -s -w "\nHTTP %{http_code} in %{time_total}s\n" \
  http://127.0.0.1:8124/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-v4-flash",
       "messages":[{"role":"user","content":"Reply with exactly: gateway-live-ok"}],
       "max_tokens":20}'
echo
echo "=== stream (SSE, first 6 lines) ==="
curl -s -N http://127.0.0.1:8124/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"count to three"}],"stream":true,"max_tokens":30}' \
  | head -6
echo
echo "=== tool-call request (must be served by a tools-capable model) ==="
curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:8124/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"what is 2+2? call the tool"}],
       "tools":[{"type":"function","function":{"name":"calculator","description":"add","parameters":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}}}}}],
       "max_tokens":40}'
