# bucker-agent

[![status](https://img.shields.io/badge/status-prototype-orange)](BUILD_PLAN.md)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![stack](https://img.shields.io/badge/stack-Postgres%20%2B%20Temporal-purple)](docs/OPERATIONS.md)

```
      ____________________________
     |  BUCKER-AGENT             |
     |  code robots that verify  |
     |  their own work           |
     |___________________________|
         \____________________/
          |  your tasks      |
          |  ----------------|------->
         _v__________________v_
        |______________________|
         \____________________/

        "nothing is trusted until it's verified"
```

**bucker-agent is a durable execution + evaluation platform for AI agents.**
You bring the agent loop; bucker makes it crash-proof, verification-gated,
cost-bounded, and benchmarkable. The LLM is the replaceable part — swap it
next year and the platform works the same. Durability, verification, and
evidence-based improvement is what the system actually *is*.

## Why bucker-agent?

| Symbol | Promise |
|---|---|
| 🧾 | **Everything is an event.** The append-only log is the truth — state is a *replay* of history, and crashes just re-derive it. |
| 🔍 | **Nothing is trusted until it's verified.** The AI's opinion never counts — real tests in an isolated sandbox decide. |
| 💰 | **Nothing overspends silently.** Budgets are enforced *before* spending, and unknown costs fail closed. |
| 🔁 | **Nothing is lost when it crashes.** Temporal resumes mid-task; every step replays deterministically. |
| 🤝 | **It asks instead of lying.** Failing tasks escalate to a human — `human_approved` is a different status than a machine pass. |
| 🧠 | **It remembers.** Finished tasks distill into facts and skills it injects next time (self-pruning, yours, local). |
| 📦 | **It can't escape.** The sandbox has no network and no privileges — even hostile code stays contained. |

> **Status: prototype (pre-1.0).** All 40 [BUILD_PLAN](BUILD_PLAN.md)
> steps have a first implementation, but the benchmark gate has no
> published live numbers yet (M2 is the go/no-go gate). Demonstrated:
> the durable core (M1 crash-resume, live), plan→work→verify end to end
> (live smoke runs), graph orchestration, human-in-the-loop, and the
> hardening pass. Treat everything beyond that as experimental.

## Known limitations

Read this before trusting bucker with real work:

- **Lite mode is NOT a security boundary.** `bucker lite` runs
  worker-produced code directly on the host (scratch dir, no container).
  Use it only for code you trust. The Docker sandbox in the full stack
  (`--network none`, unprivileged) is the real isolation boundary.
- **Lite schedules fire only while the server runs.** Lite-mode schedules
  live in SQLite and are fired by a loop inside the dashboard process —
  if it is down at a fire time, that run is skipped (never replayed,
  never duplicated). Temporal-backed schedules catch up after downtime.
- **Experimental pieces.** Graph orchestration, memory/skills, adaptive
  mode, and the MCP server are first implementations — treat their
  behavior as provisional until they accumulate live mileage.
- **No published benchmark numbers yet.** The M2 gate (paired SWE-bench
  evaluation) is the go/no-go; until it lands, treat performance claims
  as aspirational.
- **Recorded mode needs a prior live run.** The zero-key replay path
  works once recordings exist; the first run of a given task shape must
  be live with a real model.

## Contents

- [Quickstart](#quickstart-5-minutes)
- [How it works](#how-it-works)
- [Run everything in Docker](#run-everything-in-docker-zero-local-installs)
- [Use it as an API (OpenAI-compatible gateway)](#use-it-as-an-api-openai-compatible-gateway)
- [Run the pipeline end to end](#run-the-pipeline-end-to-end)
- [Choose a model](#choose-a-model)
- [Multi-step task graphs](#multi-step-task-graphs-graph-engineering)
- [Memory & skills](#memory--skills-the-harness-layer)
- [Human-in-the-loop](#human-in-the-loop)
- [Recurring tasks (schedules)](#recurring-tasks-schedules)
- [Prove the durability claim yourself](#prove-the-durability-claim-yourself)
- [Let other agents use bucker (MCP)](#let-other-agents-use-bucker-mcp)
- [CLI](#cli)
- [Usage guide](docs/USAGE.md) · [Roadmap](BUILD_PLAN.md) · [Operations](docs/OPERATIONS.md) · [Security](SECURITY.md)

## Quickstart (1 minute)

### Zero-infrastructure path (recommended for first try)

**You need:** Python 3.11+ installed. Nothing else — no Docker, no
Postgres, no Temporal, no uv. The launcher installs the Python package
with plain pip; `bucker lite` uses a SQLite database and runs tasks
in-process, so the whole platform comes up on a bare laptop.

**Windows** (PowerShell or cmd — double-click `start.bat` in File Explorer
works too):

```powershell
git clone https://github.com/abiralpokhrel-learns/bucker-agent
cd bucker-agent
.\start.bat
# or: .\start.ps1
```

**macOS / Linux:**

```bash
git clone https://github.com/abiralpokhrel-learns/bucker-agent && cd bucker-agent
./start.sh
```

The launcher: 1. finds/installs Python if missing → 2. creates `.venv` +
`pip install -e .` → 3. starts `bucker lite` → dashboard opens at
http://localhost:8123.

> **On Windows, use `start.bat` — not `start.sh`.** `start.sh` is the
> macOS/Linux script; PowerShell will not run it (it errors with "The term
> '/start.sh' is not recognized" or "cannot be loaded"). If you see that
> error, you're in the right folder — just run `.\start.bat` instead.

Demo tasks (task_type=demo) work with zero API keys and no model
configuration. `code_change` tasks need a model key in `.env`
(`DEEPSEEK_API_KEY`, or `OPENROUTER_API_KEY`) — set them and restart, or
see the full setup below.

> **⚠️ LITE MODE RUNS CODE ON YOUR MACHINE.** Lite mode executes the
> worker's code directly on the host in a scratch folder — no container
> isolation. Use it only for tasks you trust. The full Docker stack
> below is the isolated, production path (see [Known limitations](#known-limitations)).

### Full stack (Docker + Temporal — durable, isolated)

The launchers above run **lite mode** (SQLite, in-process, no Docker).
The full stack — Temporal orchestration, Postgres event store, Docker
sandbox — is bootstrapped by `bucker dev` (needs Docker Desktop running;
it installs `uv` itself):

```bash
git clone https://github.com/abiralpokhrel-learns/bucker-agent && cd bucker-agent
uv sync --extra full            # full-stack deps: Temporal + Postgres clients
uv run python -m bucker.cli dev     # Windows PowerShell: use the uv command above
# first run:  prerequisites -> .env + token -> Postgres -> migrations ->
#             Temporal + worker + dashboard (opens in your browser)
# later runs: just starts the stack
```

The same command with plain uv (if you already have it):

```bash
uv run python -m bucker.cli dev     # THE one command
```

Dashboard: http://localhost:8123 — create a task in the browser, or:

```bash
uv run python -m bucker.cli start --objective "my first durable task" --wait
uv run python -m bucker.cli events <task_id>   # the full audit trail
uv run python -m bucker.cli show    <task_id>   # state, rebuilt from events
```

### Lite mode vs the full stack

`bucker lite` (what `start.sh`/`start.bat` run) replaces the three
external services with in-process equivalents so the platform runs on a
bare laptop:

| Piece | Full stack (`bucker dev`) | Lite mode (`bucker lite`) |
|---|---|---|
| Database | PostgreSQL (Docker) | SQLite file (`bucker_lite.db`) |
| Workflow engine | Temporal server | in-process asyncio runner |
| Sandbox | network-isolated Docker container | host subprocesses in a scratch dir |
| Install | `uv` + Docker | plain `pip install -e .` |
| Schedules | Temporal scheduler | in-process scheduler over the same SQLite store (fires while the server runs; missed runs are skipped, never duplicated) |

Lite mode uses the **same** planner, worker, verifier, event store,
budget guard, retry policy, memory, dashboard, and API — only the
transport/storage layers are swapped. Tasks, graphs, and replay behave
identically; the API reports `storage: sqlite` / `temporal:
in-process` on the system page.

**The sandbox caveat:** lite mode runs worker-produced code directly on
the host in a workspace folder — no container isolation. Use it for
code you trust (demos, your own tasks, local experiments). Anything
untrusted belongs on the full stack, whose Docker sandbox is the
security boundary. `recorded` model mode is the default, so demo tasks
and replay work with **zero API keys**; add `DEEPSEEK_API_KEY` /
`OPENROUTER_API_KEY` and set `BUCKER_MODEL_MODE=live` for real model
calls, exactly like the full stack.

### Advanced (manual) setup

`bucker dev` does all of this for you; run the pieces yourself only if
you want to. `bucker setup` is the explicit bootstrap (same checks,
without starting the stack), `bucker dev --dry-run` shows the plan, and
`bucker dev --force-setup` re-runs the bootstrap on an already-ready
machine.

```bash
uv sync --extra full            # full stack: Temporal + Postgres clients
uv run python -m bucker.cli setup    # checks, .env + token, Postgres, migrations
docker compose up -d                 # Postgres (setup/dev do this too)
temporal server start-dev            # Temporal UI at http://localhost:8233
uv run python -m bucker.cli migrate  # apply schema + append-only grants
BUCKER_MODEL_MODE=live uv run python -m bucker.worker &
uv run uvicorn bucker.api.app:app --port 8123
```

(BUCKER_MODEL_MODE=live makes the worker call the real provider; without
it the worker replays stored recordings — free, but fails on any prompt it
has never seen.)

## How it works

```
  YOUR TASK
     |
     v
  [ PLANNER ]  -> typed contract: objective, budget, deadline, verifier
     |
     v
  [ WORKER  ]  -> writes the code (in the sandbox: no network, no admin)
     |
     v
  [ CRITIC  ]  -> a cheap self-review of the diff; bounded repair round
     |
     v
  [VERIFIER]  -> runs the REAL tests in Docker — the only judge
     |
     +-- passed -------------->  COMPLETED  ✅
     +-- failed (retries) ----->  NEEDS_HUMAN_REVIEW  🤝 (you decide)
     +-- out of budget/time --->  HALTED  ⏹️ (never silent)
```

Every stage writes an event (who, what, cost, when). The dashboard streams
them live. Full pipeline details in
[Run the pipeline end to end](#run-the-pipeline-end-to-end).

The same pipeline runs in two execution modes: the full stack (Temporal +
Postgres + Docker sandbox) or [lite mode](#lite-mode-vs-the-full-stack)
(SQLite + in-process runner + local sandbox) — the planner, worker,
verifier, and event store are identical; only the transport layers change.

## Features

- 🧠 **Planner** — turns a fuzzy goal into a strict typed contract
- 🛠️ **Worker** — executes inside a network-isolated Docker sandbox
- 🔍 **Verifiers** — domain-specific: code gets tested + linted; failures escalate to a human, never forced through
- 💰 **Budgets** — pre-spend guard, per-step estimates, unknown cost = fail closed
- 🧾 **Append-only event store** — enforced at the *database permission level*
- 🔁 **Deterministic replay** — record-and-replay of stored outputs, never re-invoking the model
- ⏰ **Schedules** — recurring tasks with fresh IDs per run (Temporal scheduler)
- 🕸️ **Task graphs** — DAGs with parallel waves and dependency joins
- 🧠 **Memory & skills** — durable facts + procedures, self-pruning, user-owned
- 🤝 **Human-in-the-loop** — approve/reject with notes; auditable separation of human vs machine verdicts
- 📣 **Delivery** — webhook (optionally HMAC-signed) / Telegram / Slack / Discord when a task finishes
- 🧹 **Sweeper** — `bucker sweep` finds stale and near-budget tasks, halts on request, notifies on schedule
- 💸 **Forecast** — `bucker forecast`: what YOUR task types actually cost, from recorded telemetry
- 🔁 **Batch replay** — `bucker replay --recent 25`: fleet-level reproducibility with a match rate
- 🐍 **Python SDK** — `BuckerClient` / `AsyncBuckerClient`: typed errors, `wait_for_task`, event paging (`bucker/client.py`)
- 🔎 **Verifiers** — pytest runner, citation checker, and a generic `command` verifier (make/npm/go/cargo test) via `constraints.command` or `BUCKER_SHELL_VERIFY_COMMAND`
- 🎛️ **Dashboard** — live event stream, replay, usage panel, system health
- 🩺 **Self-healing** — `bucker reconcile` re-schedules tasks that never started
- 🗄️ **Backups** — one-command Postgres + blobstore dump with a restore drill
- ⚡ **Lite mode** — `bucker lite`: SQLite + in-process runner + local sandbox;
  the whole platform on a machine with only Python (no Docker/Postgres/Temporal)



## Run everything in Docker (zero local installs)

Have Docker but don't want Python/uv/the Temporal CLI on your machine?
The whole stack — Postgres + Temporal + worker + dashboard — runs in
containers:

```bash
docker compose up --build -d        # everything, one command
docker build -t bucker-sandbox -f Dockerfile.sandbox .   # the sandbox image
open http://localhost:8123
```

(Docker Desktop proxies the docker socket into the worker container, so
sandboxes still work. `.env` is optional — dev defaults apply without it.)

## Use it as an API (OpenAI-compatible gateway)

Your `BUCKER_API_TOKEN` is an API key. Point any OpenAI-compatible client
at bucker and the gateway decides where the request goes — it is a real
inference gateway, not a passthrough. Provider selection is policy-driven
(priority / cost / latency / balanced / free-only / local-first) against a
capability registry, with auto-fallback, retries, circuit breakers, quota
tracking, streaming, and tool calling. Every call is audited as a task
with cost:

```bash
curl http://localhost:8123/v1/chat/completions \
  -H "Authorization: Bearer ${BUCKER_API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-v4-flash",
       "messages":[{"role":"user","content":"say hi"}]}'
```

What the gateway does for you:

- **Policy routing** — the default `priority` policy honors your configured
  chain (DeepSeek → Ollama → OpenRouter free); `free_only` and
  `local_first` are one env var away (`BUCKER_GATEWAY_POLICY`).
- **Capability filtering** — a request with `tools` or `stream: true` is
  only routed to models that actually support it; impossible requests are
  rejected before any provider is called.
- **Failover** — a 429/5xx/timeout retries with backoff, then falls back;
  auth errors never retry; a dead provider's circuit opens and it stops
  eating request time until it recovers.
- **Quotas** — free-tier daily caps are enforced against a durable usage
  ledger (`gateway_usage` table); an exhausted provider is skipped until
  its entitlement resets.
- **Streaming + tool calls** — SSE with normalized deltas and canonical
  `tool_calls`, whatever the upstream provider speaks.
- **Health** — `GET /health/live` (process) and `GET /health/ready`
  (database) for orchestrators; `GET /v1/models` lists what is actually
  routable right now.
- **Normalized errors** — provider internals never leak; callers get
  `{"error": {"message", "type", "code"}}` with a stable taxonomy
  (rate_limit, quota_exceeded, timeout, ...).

**One engine for every call.** Bucker's own planner/worker/critic runs go
through the *same* engine: `ModelRouter.complete()` (the stable internal
API) delegates live inference to the router, so internal work gets the
same capability filtering, policy routing, circuit breakers, and fallback
as API calls. Recorded mode is untouched — replay stays a pure recording
lookup.

**Replay never re-decides routing.** Every live inference records a
routing envelope (policy, registry config version, candidate models,
selected provider/model, fallback attempts) next to the response. Replay
returns the stored response for the same request digest — even if
provider health has changed since. Live = intelligent routing; replay =
historical reconstruction.

Architecture: `bucker/gateway/` is a self-contained package — canonical
request model, model registry, provider adapters (DeepSeek / OpenRouter /
Ollama, OpenAI-compatible), routing engine, circuit breakers, quota
manager. Adding a provider = one adapter class + registry entries; the
agent never changes.

## Windows notes (native)

Native Windows works, but the two failure modes below have bitten real
setups — both are avoidable:

1. **Install Python 3.12 with winget, not a uv-managed interpreter.** uv's
   managed interpreters live under `%APPDATA%\uv\python`, a directory that
   Windows (Smart App Control / ACLs) can lock, and uv's `.venv` launcher
   then fails with "Access is denied" / "uv trampoline failed to spawn
   Python". A python.org install lives in `AppData\Local` and never trips
   this. If you already hit it:

   ```powershell
   winget install Python.Python.3.12
   python -m venv --clear .venv          # real python.exe, no uv launcher
   uv sync --extra dev --extra full --extra llm
   ```

2. **Point uv's managed-python storage away from the locked dir** so `uv
   run` never needs to read it:

   ```powershell
   setx UV_PYTHON_INSTALL_DIR "$env:LOCALAPPDATA\uv\python"
   # open a NEW terminal afterwards
   ```

3. **Something broken and unclear?** The doctor runs on a bare python too —
   it only needs stdlib, so it works when the venv itself is dead:

   ```
   python scripts/doctor.py              # bootstrap mode
   uv run python -m scripts.doctor       # full mode
   ```

4. **Prefer WSL2 for production-adjacent work.** The sandbox (Docker on
   Linux containers), Postgres, and Temporal all behave identically but
   without Windows file-permission surprises. See
   [docs/WSL2_SETUP.md](docs/WSL2_SETUP.md) for a from-scratch guide, or use
   Docker Desktop with WSL2 backend on Windows 11 — the quickstart commands
   are the same.

## Run the pipeline end to end

This is the fastest way to see the whole system work: a tiny calculator
project with a failing test goes in, a fuzzy objective becomes a typed
contract, a model writes real code inside a network-isolated container, and
the project's own tests — not the model — decide whether it worked.

```bash
docker build -f Dockerfile.sandbox -t bucker-sandbox:latest .   # one time

uv run python -m scripts.smoke_run --live   # real model, real sandbox
uv run python -m scripts.smoke_run          # replay from recordings, free
```

The first command costs nothing if you use a local model (below). The
second command re-runs the same pipeline answering every model call from
stored recordings — no model, no network, deterministic.

### Verified results

Ran 2026-08-04 on a Windows machine (16 GB RAM, Docker Desktop, Ollama
0.32.5, `qwen2.5-coder:7b` local model):

| Run | Mode | Result | Time | Cost |
|---|---|---|---|---|
| Live | real model calls | **PASSED** — verifier: 3 tests passed | 57.7 s | $0.00 |
| Replay | stored recordings | **PASSED** — identical verdict | 3.1 s | $0.00 |

"PASSED" means the full pipeline ran: planner → contract, worker → diff,
diff applied in the sandbox, and the project's own tests passed. Your
numbers will differ by model and hardware; the outcome should not.

### Benchmark status — no live numbers yet

M2 (the go/no-go gate) has no published result yet: the paired SWE-bench
evaluation, cost/latency comparisons, and the replay-consistency proof are
built but unexecuted at scale. Producing them requires a real model budget
and looks like:

```bash
uv run python -m scripts.m2_gate --instances 25   # paired benchmark + decision rule
```

Experiment logs land in `evaluation_results/` (git-ignored) and are
exported deliberately, with recordings, when a result is published. A
rigorous negative result is a legitimate outcome — the credibility gap in
this space is exactly that nobody publishes reproducible comparisons. Until
a gate passes, the README's claims stay at "prototype", not "v1.0".

## Choose a model

bucker-agent is designed to be **model-agnostic**. The planner, worker,
critic, and verifier all talk to a provider-neutral inference layer
(`bucker/gateway`), so any model that speaks the OpenAI-compatible
chat-completions protocol plugs in with a single configuration change.
Development and testing used **Ollama for fully local inference** and
the **DeepSeek V4 API** as the primary remote provider. The providers
below marked *optional* are officially supported — the integration is
already in the codebase — but were not part of our testing, so we make
no claims about their relative performance.

### Supported model providers

| Provider | Type | Key/env | Notes |
|---|---|---|---|
| **Ollama** | Local | — (no key) | Fully local inference; used in development. No account, no cost. |
| **DeepSeek V4** | Remote (OpenAI-compatible) | `DEEPSEEK_API_KEY` | Primary remote provider during development and testing. |
| **Claude (e.g. Opus)** | Remote | `ANTHROPIC_API_KEY` | Officially supported optional provider. Plug in the key, select the model — no code changes. |
| **OpenAI (e.g. GPT-5.6 or similar)** | Remote | `OPENAI_API_KEY` | Officially supported optional provider. Plug in the key, select the model — no code changes. |
| **OpenRouter** | Remote (aggregator) | `OPENROUTER_API_KEY` | Free-tier and paid models behind one key; useful for fallback chains. |
| **Any OpenAI-compatible endpoint** | Remote/local | custom base URL | vLLM, LM Studio, Together, or your own server — same protocol, same config shape. |

### Quick configuration

Switching providers is a **one-line change** in `.env` — add the key and
select the model. No code changes required.

```bash
# ---- Local inference (recommended to start; no account, no cost) ----
ollama pull qwen2.5-coder:7b          # ~8 GB RAM; qwen2.5-coder:3b for small machines
BUCKER_MODEL=ollama/qwen2.5-coder:7b

# ---- DeepSeek V4 (primary remote provider used in development) ----
DEEPSEEK_API_KEY=sk-...
BUCKER_MODEL=deepseek/deepseek-v4-flash

# ---- Claude (optional; officially supported, not part of our testing) ----
ANTHROPIC_API_KEY=sk-ant-...
BUCKER_MODEL=anthropic/claude-opus-4-20250514   # or claude-sonnet-4-...

# ---- OpenAI (optional; officially supported, not part of our testing) ----
OPENAI_API_KEY=sk-...
BUCKER_MODEL=openai/gpt-5.6

# ---- OpenRouter (free tier or paid) ----
OPENROUTER_API_KEY=sk-or-v1-...
BUCKER_MODEL=openrouter/nvidia/nemotron-3-super-120b-a12b:free
```

Token ceilings are hard stops — an unbounded generation is an unbounded
bill. Set them once and they apply to every provider:

```bash
BUCKER_MAX_TOKENS_PLANNER=1000
BUCKER_MAX_TOKENS_WORKER=3000
```

### Model fallbacks

A dead provider should not take down a task. `BUCKER_MODEL_FALLBACKS` is a
comma-separated chain tried in order when the primary fails (provider down,
key rejected, quota exhausted):

```
BUCKER_MODEL=ollama/qwen2.5-coder:7b
BUCKER_MODEL_FALLBACKS=ollama/qwen2.5-coder:3b,openrouter/nvidia/nemotron-3-super-120b-a12b:free
```

Recorded-mode replay stays keyed to the primary model, so the chain never
affects determinism. The system page shows the chain and which providers are
reachable.

> **The swap-in promise:** because the model layer is provider-neutral, a
> user who has API keys for Claude (e.g., Opus) or OpenAI (e.g., GPT-5.6 or
> similar) can plug them in simply by adding their API key and selecting the
> model — no code changes required. The same planner, worker, verifier, and
> event store run against whichever provider you choose.

## Prove the durability claim yourself

```bash
uv run python -m tests.crash_test
```

Starts a task, hard-kills the worker (`os._exit`) between a side effect and
its event append — the nastiest possible window — restarts, and asserts the
task completes with every step recorded exactly once, the side effect
performed exactly once, and reconstructed state matching a full replay.
Exit code 0 means **M1** is demonstrated.

## Let other agents use bucker (MCP)

Any agent that speaks the Model Context Protocol — Claude Desktop, Claude
Code, Hermes, Cursor — can delegate work to bucker's verified pipeline:

```bash
uv sync --extra mcp
uv run python -m bucker.mcp.server     # stdio MCP server
```

Tools exposed: `create_task` (runs planner → worker → verifier with
budgets + audit trail), `get_task`, `list_tasks`, `replay_task` (free,
deterministic re-run), `cancel_task`, `system_status`. Register it in your
agent as a stdio MCP server; it talks to Postgres + Temporal directly, no
HTTP server needed.

## Multi-step task graphs (graph engineering)

A graph is a DAG of steps — each step a full verified pipeline
(planner → worker → critic → verifier). Independent steps run **in
parallel** (Temporal child workflows); a step starts only after everything
it depends on finished. The whole graph is one task, so the audit trail
covers the run and each step replays independently.

```bash
bucker graph run examples/graph_demo.json
# or over the API:
curl -X POST http://localhost:8123/graphs -H "Content-Type: application/json" \
     -d @examples/graph_demo.json
```

```json
{
  "name": "calc-refactor-demo",
  "steps": [
    {"id": "add-sub", "objective": "Add sub() to calc.py"},
    {"id": "add-mul", "objective": "Add mul() to calc2.py"},
    {"id": "verify-both", "objective": "Make check.py pass",
     "depends_on": ["add-sub", "add-mul"]}
  ]
}
```

`add-sub` and `add-mul` run at the same time; `verify-both` waits for
both (the join). Validation is strict and runs before anything launches:
duplicate ids, unknown dependencies, and cycles are rejected with 400.
Per-step budgets and retries apply; `fail_fast` stops scheduling after a
failed step.

## Memory & skills (the harness layer)

bucker keeps learning across sessions, Hermes-style, with local files you
own (in `memory/` and `skills/`, git-ignored):

| memory | what | where |
|---|---|---|
| **working** | the task's contract + workspace + event stream | per task (durable) |
| **procedural** | skills: procedures the worker follows when an objective matches | `skills/<name>/SKILL.md` |
| **semantic** | facts about the user/project, injected into planner+worker prompts | `memory/*.md` |
| **episodic** | the append-only event store — every run, exactly once | Postgres |

```bash
bucker memory add "this project's tests run with pytest"
bucker memory list / search <kw>
bucker memory consolidate <task_id>   # distill a run into facts (idempotent)
bucker skills list / show <name>
bucker skills new fix-failing-tests \
  --description "Repair a failing test suite" \
  --procedure "1. run the tests\n2. read the first failure\n3. fix the root cause"
```

How it works: when a task starts, skills whose description overlaps the
objective are injected into the worker prompt (procedural memory becomes
part of working memory), and relevant facts are injected into both the
planner and worker prompts. A failed verification run can be consolidated
into a durable fact and a *skill proposal* — self-improvement, but a human
always says yes before a skill is created.

Memory is self-curating: finished tasks are consolidated into facts
automatically (BUCKER_AUTO_CONSOLIDATE), and the store is bounded by a
dedupe+cap pass:

```bash
bucker memory status   # audit: counts by source, oldest/newest
bucker memory prune    # dedupe identical facts + cap the store
```

## Human-in-the-loop

When the verifier cannot pass and the policy escalates, the task lands in
`needs_human_review` — and the human is the judge. Approve or reject it
with a note; the verdict is append-only and the task becomes
`human_approved` / `human_rejected`, deliberately distinct from machine
verdicts so the audit trail can never confuse the two.

```bash
curl -X POST http://localhost:8123/tasks/<id>/approve?note=looks+right
curl -X POST http://localhost:8123/tasks/<id>/reject?note=wrong+approach
```

The task dashboard shows approve/reject buttons on escalated tasks, the
self-critique verdicts per attempt, and (for graphs) each step's status.

## Delivery (where the user is)

Scheduled and graph runs announce their outcome to a webhook or Telegram
— opt-in, no-op when unconfigured:

```
TELEGRAM_BOT_TOKEN=...   TELEGRAM_CHAT_ID=...    # Telegram delivery
BUCKER_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...   # Slack
BUCKER_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...  # Discord
BUCKER_NOTIFY_WEBHOOK_URL=https://...            # generic webhook
BUCKER_NOTIFY_WEBHOOK_SECRET=whsec_...           # optional HMAC signing
```

One channel per event (precedence: Telegram > Slack > Discord >
webhook); all opt-in, no-op when unconfigured. The generic webhook body
carries structured fields (`event`, `status`, `task_id`, `cost_usd`, …)
next to the prose `text`. With a secret configured, every webhook POST
is signed — receivers verify it with:

```python
from bucker.core.notify import verify_webhook_signature

assert verify_webhook_signature(secret, request.headers["X-Bucker-Signature"], raw_body)
```

## Tracing (LLM ops)

Every run is a trajectory: model calls with tokens and cost, tool calls,
verdicts, in order. Export it for debugging or audits:

```bash
bucker export <task_id>                # markdown
bucker export <task_id> --format json  # full structured trace
bucker export <task_id> --format jsonl # one event per line
```

Or from the API: `GET /tasks/{id}/trajectory?format=md|json|jsonl`. The
task dashboard links to it. Trajectories are a projection of the
append-only event store — exporting never mutates anything.

## Multi-platform access (gateway)

One platform, four doors:

- **Web dashboard** — `uv run uvicorn bucker.api.app:app --port 8123`
  (overview, tasks, models, memory, skills, schedules, system)
- **CLI** — `bucker` (start/show/events/replay/tasks/usage/models/
  providers/setup/memory/skills/schedules/export/doctor)
- **MCP** — any agent that speaks MCP (Claude, Hermes, Cursor) delegates
  tasks to bucker's verified pipeline (`uv run python -m bucker.mcp.server`)
- **REST API** — `/docs` for the full OpenAPI surface

All data lives locally: Postgres (events, telemetry), the blob store
(recordings), and markdown (memory, skills). No cloud, no telemetry home.

## Models & providers — free, paid, local

bucker runs any model the router can reach, from three tiers:

| tier | what | cost | needs |
|---|---|---|---|
| **local** | Ollama models on your machine | $0, private | Ollama installed, `ollama pull <model>` |
| **free** | hosted free tiers via OpenRouter | $0 | `OPENROUTER_API_KEY` (free account) |
| **paid** | hosted models via OpenRouter | per token | key + credit |

```bash
bucker models          # browse the catalog with tiers + configured markers
bucker providers       # live status: what Ollama has pulled, key shape
bucker setup           # one-command env bootstrap: checks, .env, database
bucker dev             # start the whole stack (temporal + worker + dashboard)
bucker setup-wizard    # propose a free-first model chain (dry run)
bucker setup-wizard --apply   # ...and writes it into .env (other lines untouched)
```

The wizard proposes a deterministic free-first chain — best local coder →
best free hosted → best paid — and `BUCKER_MODEL_FALLBACKS` feeds that
chain into the gateway engine's default `priority` policy: when a provider
fails (dead key, quota, outage) the engine falls through with retries,
circuit breakers, and capability filtering on top. Adaptive retries switch
models through the same registry — a model requirement, never a
hardcoded list. Replay determinism is unaffected: replays stay keyed to
the primary model and never re-decide routing.

The dashboard's `/models-page` shows the whole story: tier badges,
what's configured, provider health, and the suggested chain.

## Recurring tasks (schedules)

The same verified pipeline, on a cron — "verify the deploy every morning":

```bash
bucker schedules list
bucker schedules create nightly-bench --template code-fix --cron "0 9 * * 1-5"
bucker schedules pause nightly-bench
bucker schedules delete nightly-bench
```

Each run mints a fresh task (new audit trail, new idempotency keys) and
runs the full plan → work → verify loop. Schedules live in Temporal on
the full stack, and in the lite SQLite store in lite mode — same CLI,
same API (`POST /schedules`, `POST /schedules/{id}/pause`), same cron
semantics (5 fields, `TZ=Area/City` prefix honored; evaluated per its
timezone). Manage them from the dashboard at `/schedules-page`.

## Watch a task from the terminal

```bash
bucker start --code --objective "..."     # note the task id
bucker watch <task_id>                    # live-tail events until a verdict
bucker wait  <task_id> --quiet            # scripts: exit 0 pass / 1 fail / 2 escalated
```

`watch` prints each event as it lands and exits with the verdict's class;
`wait` is the quiet variant for shell scripts and CI.

## Python SDK

```python
from bucker.client import BuckerClient

with BuckerClient() as bucker:                      # http://localhost:8123, dev token
    task = bucker.create_task("Add a subtract function to calc.py",
                              budget_usd=0.25)
    result = bucker.wait_for_task(task["task_id"], timeout_s=900)
    print(result["status"], result["cost_usd"])

    for event in bucker.iter_events(task["task_id"]):
        print(event["event_type"])
```

`AsyncBuckerClient` mirrors the whole surface for asyncio code. Errors
are typed (`NotFoundError`, `ConflictError`, `ValidationError`, …) and
`wait_for_task` raises `WaitTimeoutError` instead of hanging forever.
Works identically against the full stack and lite mode — both serve the
same API.

## Task templates

The new-task form (and schedules) start from named presets — code-fix,
feature-add, research, data-extraction, demo — each with a sensible
objective, budget and deadline. `bucker templates` lists them; one click
on the form fills everything in.

## CLI

```bash
bucker start --code --objective "..." --budget-usd 0.5 --wait
bucker tasks                # recent tasks: status, cost, tokens (--format json|csv)
bucker usage                # tokens/cost by model and stage (--format json)
bucker forecast             # cost/token distributions per task type, from YOUR data
bucker show <id>            # state rebuilt from events
bucker events <id>          # the full audit trail
bucker watch <id>           # live-tail events until a verdict
bucker wait <id> --quiet    # script-friendly block (exit 0/1/2)
bucker replay <id>          # deterministic re-run from recordings
bucker replay --recent 25   # batch: match rate across recent completed tasks
bucker sweep                # stale + near-budget triage (exit 1 = actionable)
bucker sweep --halt         # ...and record TaskFailed for the stale ones
bucker templates            # task presets
bucker schedules list       # recurring tasks (lite + full stack)
bucker version              # version + configured mode
bucker doctor               # diagnose a broken setup
```

## Web UI + HTTP API

The dashboard is server-rendered HTML — no frontend build step.

```bash
uv run uvicorn bucker.api.app:app --port 8123
open http://localhost:8123/          # aggregate dashboard
open http://localhost:8123/tasks/new # create a task from the browser
```

| Page | What it shows |
|---|---|
| `/` | success rate, spend, tasks per day, recent tasks |
| `/tasks/new` | form to create a task (code or demo) |
| `/tasks/{id}/dashboard` | one task: plan, timeline, verdict, cost — plus **re-run** and **cancel** buttons |
| `/tasks/{id}/replay` | re-run a task from its recordings, free |
| `/usage` | **usage**: tokens + cost by model, by pipeline stage, per day |
| `/system` | **control center**: model chain, provider reachability, Postgres/Docker/Temporal/sandbox health, verifier registry, storage |

The same surface works as a JSON API:

```bash
# create a real code task (the planner picks the verifier)
curl -X POST "http://localhost:8123/tasks?objective=Add%20a%20subtract%20function%20to%20calc.py&budget_usd=0.50&deadline_minutes=10"

# list tasks with cost + event counts; filter by status
curl "http://localhost:8123/tasks?status=completed"

# the audit trail, and a deterministic replay from recordings
curl "http://localhost:8123/tasks/<id>/events"
curl -X POST "http://localhost:8123/tasks/<id>/replay"

# control: re-run a finished task (new task, same objective), cancel a running one
curl -X POST "http://localhost:8123/tasks/<id>/rerun"
curl -X POST "http://localhost:8123/tasks/<id>/cancel"

# system health as JSON
curl "http://localhost:8123/api/system"
```

A re-run is honest by construction: the original event stream is append-only
and never mutated — a re-run is a new task that shares the objective.
Cancelling terminates the Temporal workflow (requires Temporal to be up).

`task_type=demo` keeps the five-step Phase 0 workflow (with the `noop`
verifier) for the durability demo; anything else runs the real pipeline.
The CLI mirrors this: `bucker.cli start --code` runs the real pipeline,
plain `start` runs the demo.

### Adaptive retries (M3)

Wired in but off by default, so it can be A/B tested against fixed retry:

```bash
# fixed retry (default): every attempt re-prompts with the verifier's diagnostics
uv run python -m bucker.cli start --code --objective "..." --max-retries 3

# adaptive: on repeated failure, switch model / chunk / clarify instead
uv run python -m bucker.cli start --code --objective "..." --adaptive

# same, via the API
curl -X POST "http://localhost:8123/tasks?objective=...&adaptive=true"
```

## Testing

```bash
uv run python -m pytest                        # pure tests, no infra needed
BUCKER_TEST_DATABASE_URL=postgresql://postgres:***@localhost:5432/bucker uv run python -m pytest
```

Database tests skip automatically when that variable is unset, so a fresh
clone tests green with nothing running.

Something broken and you're not sure what? `uv run python -m scripts.doctor`
checks uv, the venv, imports, config, Docker, Postgres, Temporal and Ollama,
and fails cleanly with an actionable hint for each broken piece.

**Always invoke tools as `python -m <tool>`, not via the generated `.exe`
shims.** `uv` writes small unsigned launchers (`pytest.exe`, `ruff.exe`)
into `.venv/Scripts/`. Windows Smart App Control blocks them outright
(`os error 4551`), and they also break if the project folder is ever moved,
since each one hardcodes an absolute path. Going through `python -m`
sidesteps both and behaves identically on Linux and macOS.

## Project layout

```
bucker/
  core/          event store, state fold, snapshots, blob storage, telemetry,
                 budget enforcement                                [HAND]
  contracts/     typed Task + WorkerResult — JSON Schema + pydantic
  workflows/     Temporal workflow definitions (deterministic!)    [HAND]
  activities/    all side effects live here, idempotent by key     [HAND]
  router/        model router — model name is config, never code
  verifiers/     plugin interface + python_test_runner + citation_checker
  replay/        deterministic record-replay engine                [HAND]
  bench/         baseline agent, SWE-bench integration, paired runner,
                 stats (McNemar + bootstrap), scorer, promotion, regression gate
  adaptive.py    strategy selector for adaptive retry
  api/           FastAPI surface (tasks, events, replay, dashboard)
  prompts/       versioned prompt templates
  sandbox/       Docker sandbox with network isolation              [HAND]
tests/
  crash_test.py   the M1 durability proof
migrations/       SQL, append-only grants included
scripts/
  smoke_run.py    end-to-end pipeline verification
  diagnose.py     failure taxonomy from event streams
  m2_gate.py      paired benchmark + decision rule
```

## The vibe-code rule

Files marked `[HAND]` in their module docstring are the ones where a subtle
bug silently poisons everything above them: the event store, the state
fold, idempotency, replay, verifiers, and the stats module. Read every line
of those.

Everything else — configs, CI, dashboards, plumbing — is fine to generate.

The meta-rule: **generated code is untrusted worker output, and the test
suite is its verifier.** Nothing merges without passing, no matter who
wrote it. That is the same principle the platform applies to its own
agents, applied to its own construction.

## Roadmap

| Phase | Weeks | Gate |
|---|---|---|
| 0 — Durable core | 1–3 | **M1:** `kill -9` → resume, zero data loss ✅ scaffolded |
| 1 — Plan→Work→Verify + benchmark | 4–12 | **M2 (go/no-go):** beat a single-agent baseline on the same model, or stop and rethink |
| 2 — Scheduling & observability | 13–17 | **M3:** adaptive planning measurably cuts repeat-failure rate |
| 3 — Second domain & promotion pipeline | 18–26 | **M4:** promote → regress → rollback proven end to end |

M2 is a real gate with a real kill switch. The benchmark result gets
published either way — a rigorous negative result is still a contribution,
and the credibility gap in this space is exactly that nobody publishes
reproducible comparisons.

## Contributing

bucker-agent is open source and welcomes contributors. Please read the
project's own rules before opening a PR:

- [CONTRIBUTING.md](CONTRIBUTING.md) — how to work here
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — community standards
- [SECURITY.md](SECURITY.md) — trust model and how to report vulnerabilities
- [CHANGELOG.md](CHANGELOG.md) — what changed, release by release
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — what to change before exposing it
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — backup/restore drill, monitoring,
  log retention, migration rollback policy, incident runbook, M2 gate procedure

The meta-rule is simple: **generated code is untrusted worker output, and
the test suite is its verifier.** Nothing merges without passing, no matter
who wrote it — that is the same principle the platform applies to its own
agents, applied to its own construction.

## License

Apache-2.0. See [LICENSE](LICENSE).
