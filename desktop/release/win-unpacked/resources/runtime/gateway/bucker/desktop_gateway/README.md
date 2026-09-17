# Isolated desktop gateway

Run from the repository (or installed package):

```
python -m bucker.desktop_gateway --port 49152
```

`--port 0` asks the OS for an available port. There is deliberately no `--host` option: the socket always binds `127.0.0.1`.

## Parent protocol

Parent supplies environment variables, never CLI credentials:

- `BUCKER_DESKTOP_TOKEN`: nonempty ASCII random ephemeral bearer token.
- `BUCKER_DESKTOP_CONNECTIONS`: nonempty JSON array. Each entry must have exactly `provider`, `base_url`, `api_key`, `model`, `context` (positive integer), `max_output` (positive integer), `free` (true), and `tools` (boolean).
- Parent should set `PYTHON_DOTENV_DISABLED=1`; the package also forces it **before importing core gateway modules**. `bucker.config` uses an absolute repository `.env` path, so changing cwd alone does not isolate configuration. Tested with the installed python-dotenv 1.2+ disable switch. Use a fresh process, not an interpreter that already imported other Bucker modules.

Configured order is fallback priority. Duplicate provider/model entries and conflicting credentials for a single provider are rejected. Provider URLs must exactly equal:

| Provider | Base URL |
| --- | --- |
| openrouter | https://openrouter.ai/api/v1 |
| gemini | https://generativelanguage.googleapis.com/v1beta/openai |
| groq | https://api.groq.com/openai/v1 |
| cerebras | https://api.cerebras.ai/v1 |
| mistral | https://api.mistral.ai/v1 |
| huggingface | https://router.huggingface.co/v1 |

OpenRouter models must end in `:free`. `free=true` is the **user's free-billing confirmation**, not proof of remaining credits or cost. No catalog/default providers are added. HTTP clients ignore proxy environment settings and do not follow redirects.

Stdout is JSON-lines lifecycle output. After the socket is serving:

```json
{"status":"ready","host":"127.0.0.1","port":49152,"pid":1234}
```

The PID is the actual serving process; Windows venv launchers may have a different parent PID. Startup configuration/bind failure emits `{"status":"error","error":{"type":"configuration_error","message":"invalid desktop gateway configuration"}}` (or `bind_error`) and exits nonzero. No credentials/raw configuration are printed.

## HTTP

All paths, including unknown paths, require `Authorization: Bearer <token>` except `/health/live`. No docs/OpenAPI routes or CORS are enabled.

- `GET /health/live`: process liveness.
- `GET /health/ready`: local service readiness; explicitly **not** credential/provider verification.
- `GET /v1/models`: `desktop-auto` alias followed by configured canonical `provider/model` IDs.
- `POST /v1/chat/completions`: text-only OpenAI messages, including assistant tool calls and tool results. `desktop-auto`/omitted model maps to the engine's automatic choice. Explicit canonical IDs must be configured; they are preferences and can fall back.
- `GET /usage`: memory-only session ledger (last 200 request metadata entries plus lifetime session counters). No prompts/tool arguments are retained. Records served provider/model, failed candidate attempts, outcomes, reported tokens. `remaining_quota` and `cost_usd` remain null. Totals sum only observed tokens, **not** provider billing or complete usage for interrupted/unreported responses.

Supported options: `temperature`, `top_p`, `max_tokens`, `tools`, `tool_choice`, `response_format`, `stream`, `stream_options.include_usage`. Unsupported top-level/message fields (including `n`, `parallel_tool_calls`, `stop`, multimodal content, routing/free overrides) return a redacted 422 rather than being silently discarded. Model metadata is routing/display information, not a tokenizer-based context/output-size guarantee.

Responses consistently use `model: desktop-auto`. Nonstreaming responses include `X-Request-Id`, `X-Served-Model`, `X-Served-Provider`. Streaming emits OpenAI chunks, terminal finish/optional reported usage, then `[DONE]`; served model is available in `/usage` after completion. Tool identity/name is emitted once per index to avoid SDK concatenation corruption. A post-delta failure emits a structured SSE `error`, then `[DONE]`; **never retry tool execution or transparently replace the provider after partial output**.

Reuses `RouterEngine`, `ModelRegistry`, `GatewayModel`, and `OpenAICompatAdapter`. It does not import `bucker.api` or use database, Temporal, project `.env`, default catalog, or durable usage storage.

## Verification

```
.venv/Scripts/python.exe -m pytest tests/test_desktop_gateway.py tests/test_gateway_engine.py tests/test_gateway.py -ra
.venv/Scripts/python.exe -m ruff check bucker/desktop_gateway tests/test_desktop_gateway.py
```

Tests use real engine/adapters and mocked HTTP transports; subprocess readiness uses a real loopback server. No real provider credentials/billing calls are made.
