# bucker-agent — Detailed Usage Guide

A step-by-step manual for the whole system. If you only ever run one
thing, it is this:

```bash
git clone https://github.com/abiralpokhrel-learns/bucker-agent && cd bucker-agent
./start.sh                # Windows: start.bat
```

Everything below explains what that does and how to go further.

---

## 0. What you are running

bucker-agent is a **durable agent execution platform**. You give it a
task; it plans, writes, double-checks, and runs tests in a locked-down
sandbox — and it can prove nothing was lost if the machine dies mid-task.

Four pieces work together:

| piece | what it does | where it runs |
|---|---|---|
| **Temporal** | durable workflow manager (retries, timers, recovery) | `docker compose` or local CLI |
| **Worker** | the agent: planner → worker → critic → verifier | local process |
| **API / dashboard** | web UI + REST + OpenAI-compatible gateway | local process, port 8123 |
| **Postgres** | the append-only event log (the source of truth) | `docker compose` |

The key idea: **everything is an event**. The database stores an
append-only log; task state is *rebuilt* from that log. A crash anywhere
just means the log gets replayed. Model calls are also recorded, so a
re-run is free and deterministic.

---

## 1. Requirements

Only two things:

- **Docker Desktop** (running) — sandbox + Postgres + optional Temporal
- **uv** — Python package manager (the launcher installs it for you if
  missing: `winget install astral-sh.uv` on Windows,
  `curl -LsSf https://astral.sh/uv/install.sh | sh` elsewhere)

No Python install needed — uv manages that.

---

## 2. Install and first run

```bash
git clone https://github.com/abiralpokhrel-learns/bucker-agent
cd bucker-agent
./start.sh                # Windows: start.bat
```

On the **first run** this automatically:

1. checks prerequisites (Docker running? uv present? — prompts to act if not)
2. creates `.env` with a generated `BUCKER_API_TOKEN`
3. starts Postgres
4. applies database migrations
5. starts Temporal, the worker, and the dashboard
6. opens the dashboard in your browser

On **later runs** it skips straight to starting the stack. The same
command with plain uv:

```bash
uv run python -m bucker.cli dev
```

Useful variations:

```bash
uv run python -m bucker.cli dev --dry-run      # what would happen, start nothing
uv run python -m bucker.cli dev --force-setup  # re-run bootstrap even if ready
uv run python -m bucker.cli dev --no-live      # worker uses recordings, no model calls
uv run python -m bucker.cli dev --no-browser   # don't open the dashboard
```

---

## 3. Choosing a model (free / local / paid)

The system is model-agnostic. You configure the chain in `.env`:

```bash
# 1. LOCAL — free, private (install Ollama, pull a coder model)
BUCKER_MODEL=ollama/qwen2.5-coder:7b
# (ollama pull qwen2.5-coder:7b)

# 2. FREE hosted — via OpenRouter free tier
OPENROUTER_API_KEY=sk-or-v1-...
BUCKER_MODEL=openrouter/nvidia/nemotron-3-super-120b-a12b:free

# 3. PAID — DeepSeek (direct), or any paid OpenRouter model
DEEPSEEK_API_KEY=sk-...
BUCKER_MODEL=deepseek/deepseek-v4-flash

# Fallbacks: tried in order when a provider fails (dead key, quota, outage)
BUCKER_MODEL_FALLBACKS=ollama/qwen2.5-coder:7b,openrouter/nvidia/nemotron-3-super-120b-a12b:free
```

The wizard proposes a sensible free-first chain for you:

```bash
uv run python -m bucker.cli setup-wizard            # dry run
uv run python -m bucker.cli setup-wizard --apply    # writes .env
```

Inspect what is configured and reachable:

```bash
uv run python -m bucker.cli models       # catalog: tiers, capabilities, config
uv run python -m bucker.cli providers    # live status: Ollama pulls, key shape
```

> Model names are **provider-prefixed** (`provider/model`). The `:free`
> suffix on OpenRouter models means the free tier. Free tiers are treated
> as quotas — daily caps are enforced, and an exhausted provider is
> skipped until it resets.

---

## 4. Give it your first task

Dashboard (opened automatically): <http://localhost:8123>

Click **New task** and type, for example:

```
create a file called hello.py that prints "hello from the robot"
```

Or from the terminal:

```bash
# quick demo (no code pipeline)
uv run python -m bucker.cli start --objective "my first durable task" --wait

# the REAL pipeline: plan -> work -> critique -> verify in the sandbox
uv run python -m bucker.cli start --code \
  --objective "write a python function that returns the nth fibonacci number" \
  --verifier python_test_runner --wait
```

Watch the status change as it works:

| status | meaning |
|---|---|
| `pending` | waiting for a worker |
| `in_progress` | working right now |
| `verification_failed` | tests failed, retrying (with diagnostics) |
| `completed` | the verifier says it passed |
| `needs_human_review` | stuck — it needs YOUR decision |
| `human_approved` / `human_rejected` | your verdict |
| `halted` | ran out of budget or time |
| `failed` | gave up |

### Useful task commands

```bash
uv run python -m bucker.cli tasks                     # recent tasks
uv run python -m bucker.cli tasks --limit 10
uv run python -m bucker.cli show <task_id>            # state, rebuilt from events
uv run python -m bucker.cli events <task_id>          # the full audit trail
```

---

## 5. Verifiers — how the robot checks its own work

The model never declares itself successful. A **verifier** checks the
actual output:

- `noop` — accept anything (demo only)
- `python_test_runner` — runs the produced tests in a **sandboxed
  Docker container with no network**, reports pass/fail with diagnostics
- `citation_checker` — checks that claims are grounded in citations

```bash
uv run python -m bucker.cli start --code \
  --objective "add a subtract function with a test" \
  --verifier python_test_runner --wait
```

On failure, the diagnostics are fed back and the worker retries (bounded
by `--max-retries`, default 2). With `--adaptive`, the retry *strategy*
changes on repeated failure (different model / chunk the task / ask you
for clarification) instead of re-rolling the same prompt.

---

## 6. Budgets and deadlines

Hard ceilings, enforced in the workflow:

```bash
uv run python -m bucker.cli start --code \
  --objective "..." \
  --budget-usd 0.50 \
  --deadline-minutes 30 \
  --max-retries 3
```

- `--budget-usd` — cost ceiling; when cost is *unknown* (unpriced model)
  and a budget is set, the task **fails closed** rather than spend blind
- `--deadline-minutes` — time ceiling

A task that hits either becomes `halted`.

---

## 7. When the robot asks you (human-in-the-loop)

A `needs_human_review` task pauses and waits. In the dashboard, open the
task and click **approve** or **reject** (add a note — it is recorded).
From the API:

```bash
curl -X POST http://localhost:8123/tasks/<task_id>/approve \
  -H "Authorization: Bearer $BUCKER_API_TOKEN" -d '{"note":"looks good"}'
curl -X POST http://localhost:8123/tasks/<task_id>/reject \
  -H "Authorization: Bearer $BUCKER_API_TOKEN" -d '{"note":"please retry with tests"}'
```

Your verdict is an event in the log — the task's history is complete.

---

## 8. Replay — deterministic re-runs

Every model call is recorded. `replay` re-runs a task from the stored
recordings — **no model, no network, no cost** — and must produce the
identical result:

```bash
uv run python -m bucker.cli replay <task_id>
```

This is the determinism guarantee: live runs make *intelligent routing*
decisions (the gateway picks providers); replays are *historical
reconstruction* (they never re-decide, never contact a provider).

Recorded mode is the same idea applied to the worker:

```bash
uv run python -m bucker.cli dev --no-live     # worker replays recordings
```

---

## 9. Prove the durability claim yourself

The M1 durability test hard-kills the worker (`os._exit`) in the
nastiest possible window — after a side effect, before its event append —
restarts it, and asserts: every step recorded exactly once, the side
effect performed exactly once, and reconstructed state matching a full
replay:

```bash
uv run python -m tests.crash_test
```

Exit code 0 means the core promise ("nothing is lost when it crashes")
is demonstrated on your machine.

---

## 10. Multi-step task graphs

A task DAG runs steps in waves, each step its own verified task:

```bash
uv run python -m bucker.cli graph run graphs/my-spec.json
```

The spec declares nodes and dependencies; the workflow executes them in
topological waves with per-node budgets + verification. See the README's
"Multi-step task graphs" section for the JSON format.

---

## 11. Recurring tasks (schedules)

```bash
uv run python -m bucker.cli schedules list
uv run python -m bucker.cli schedules create nightly-bench \
  --template code-fix --cron "0 9 * * 1-5" \
  --budget-usd 2.0
uv run python -m bucker.cli schedules pause nightly-bench
uv run python -m bucker.cli schedules delete nightly-bench
```

Each run is a fresh task through the same verified pipeline. (The CLI
also has `reconcile` — re-schedules tasks whose workflow never started,
e.g. after a restart.)

---

## 12. Memory & skills — the harness layer

```bash
uv run python -m bucker.cli memory add --source terminal "the deploy script lives at scripts/deploy.sh"
uv run python -m bucker.cli memory list

uv run python -m bucker.cli skills            # procedural skills the worker follows
```

Memory is durable facts across sessions; skills are repeatable
procedures. Both live in the project data directory (plain markdown/SQLite
— no cloud, no telemetry home).

---

## 13. Templates

```bash
uv run python -m bucker.cli templates
```

Built-ins: `code-fix`, `feature-add`, `research`, `data-extraction`,
`demo`. Schedules and the dashboard can create tasks from a template so
recurring work has a stable shape.

---

## 14. The OpenAI-compatible gateway (`/v1`)

The API doubles as a **policy-driven inference gateway**. Any
OpenAI-compatible client can point at it; the gateway decides provider,
fallback, retries, quotas, circuit breakers:

```bash
curl http://localhost:8123/v1/chat/completions \
  -H "Authorization: Bearer ${BUCKER_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-v4-flash",
       "messages":[{"role":"user","content":"say hi"}]}'
```

- `GET /v1/models` — what is actually routable right now
- `POST /v1/chat/completions` — with `"stream": true` for SSE; tool
  calls come back as canonical `tool_calls`
- `GET /health/live` (process) and `GET /health/ready` (database) —
  for orchestrators
- Routing policies: `priority` (default, honors your chain), `free_only`,
  `local_first`, `cost`, `latency`, `balanced` — via
  `BUCKER_GATEWAY_POLICY`
- Every call is audited: a task row + event + usage record with real cost

Normalized errors: `{"error": {"message", "type", "code"}}` with a
stable taxonomy (`rate_limit`, `quota_exceeded`, `timeout`, ...) —
provider internals never leak.

---

## 15. Let other agents use bucker (MCP)

Any MCP-capable agent (Claude, Cursor, Hermes, ...) can drive bucker as
a tool server:

```bash
uv run python -m bucker.mcp.server
```

Exposes `create_task`, `get_task`, `list_tasks`, `replay_task`,
`cancel_task`, `system_status`. Point your agent at it (stdio transport),
then it can say "run this task through the verified pipeline" and get a
durable, verifiable, auditable result back.

---

## 16. Usage & cost tracking

```bash
uv run python -m bucker.cli usage            # tokens + cost by model / stage / day

# export one task's full trajectory (LLM-ops trace) for inspection:
uv run python -m bucker.cli export <task_id> --format jsonl > trajectory.jsonl
```

The dashboard's **Usage** page shows the same numbers. Telemetry records
model, provider, tokens, cost, latency, and the routing decision for
every inference.

---

## 17. Dashboard pages

| page | what it shows |
|---|---|
| `/` | task list + health |
| `/tasks/new` | create a task |
| `/tasks/<id>/dashboard` | live event stream, status, verdicts |
| `/tasks/<id>/replay` | deterministic re-run view |
| `/usage` | tokens / cost by model and stage |
| `/system` | providers, model chain, health |
| `/memory-page` | the harness layer |

---

## 18. Troubleshooting

| symptom | fix |
|---|---|
| `Unable to find image 'bucker-sandbox:latest'` | `docker build -t bucker-sandbox:latest -f Dockerfile.sandbox .` (the dev/setup path does this on demand; CI builds it too) |
| `Docker is not running` | start Docker Desktop, re-run `bucker dev` |
| everything starts but tasks stay `pending` | check the worker: `uv run python -m bucker.worker` in a terminal and read its output |
| model errors / empty responses | check `.env` keys (`models` + `providers` commands), try `setup-wizard --apply` |
| `schedule_failed` on a schedule | usually the sandbox image missing or Temporal down — see first row; `reconcile` re-schedules strays |
| postgres won't start | `docker ps` — port 5432 conflict? `docker compose down && docker compose up -d` |
| re-run the whole bootstrap from scratch | `uv run python -m bucker.cli dev --force-setup` |

Full health check: `uv run python -m scripts.doctor`

---

## 19. Production notes

- **`BUCKER_MODEL_MODE=live`** — the worker calls real providers.
  Default (and CI) is `recorded`: deterministic, free, but fails on any
  prompt it has never seen.
- Migrations are idempotent and re-applied by `bucker migrate`; the
  append-only log is the truth — never edit rows, append events.
- The gateway engine (`bucker/gateway/`) is the single routing layer:
  internal worker calls and `/v1` calls both go through it. Adding a
  provider = one adapter + registry entries; the agent code never changes.
- Docker compose deploys the whole stack (Postgres + Temporal + app +
  sandbox); see the README's "Run everything in Docker" section.
