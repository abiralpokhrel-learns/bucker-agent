# bucker-agent

**Nothing is trusted until it's verified, nothing is lost when it crashes,
nothing overspends silently, and nothing changes in production until it's
proven better.**

bucker-agent is a durable execution and evaluation platform for AI agents.
It is not another agent framework — it is the layer *underneath* one. You
bring your own agent loop; bucker makes it crash-proof, verification-gated,
cost-bounded, and benchmarkable.

The LLM is the replaceable part. Swap it for a stronger model next year and
the platform works the same way. The durability, verification, and
evidence-based improvement is what the system actually *is*.

> **Status: prototype (pre-1.0).** All 40 BUILD_PLAN steps have a first
> implementation, but several subsystems are unproven in production: the
> benchmark gate, promotion pipeline, and replay regression gate have no
> published live numbers yet — M2 is a go/no-go gate precisely because of
> that. What is demonstrated: the durable core (M1, via the crash test) and
> plan→work→verify end to end (smoke run, below). Treat everything beyond
> that as experimental until live benchmark evidence exists. See
> [`BUILD_PLAN.md`](BUILD_PLAN.md) for the roadmap and which steps are
> ticked.

---

## What it does

1. **Everything is an event; the event log is the truth.** No "current
   state" is stored as the primary thing — state is a replay of history.
   Crashes aren't catastrophic: the system re-derives where it was and picks
   up. The `events` table is append-only *at the database permission level*,
   not by convention.
2. **A Planner turns a fuzzy goal into a strict, typed contract** (task
   type, objective, constraints, budget, deadline, verifier). Validation
   failures are recorded as events, never silently dropped.
3. **A Worker executes — and its output is never trusted on its own.**
4. **A domain-specific Verifier checks the result.** Code gets tested and
   linted. There is no universal "is this good?" function; each domain plugs
   in its own objective check. Repeated failure escalates to a human instead
   of being forced through.
5. **Every step is logged with cost, time, and outcome, and can be replayed
   exactly.** Determinism comes from record-and-replay of stored model/tool
   outputs — never by re-invoking the model.
6. **Improvements are proposed, benchmarked, and only promoted if they
   win.** No live self-modification. Human approval, rollback retained.

## Quickstart (5 minutes)

**You need:** Python 3.12+, Docker, and the
[Temporal CLI](https://docs.temporal.io/cli).

```bash
git clone <your-repo-url> && cd bucker-agent
cp .env.example .env

docker compose up -d                 # Postgres
temporal server start-dev            # Temporal UI at http://localhost:8233

uv sync --extra dev
uv run python -m bucker.cli migrate  # apply schema + append-only grants

# start the worker. BUCKER_MODEL_MODE=live makes it call the real provider;
# without it the worker replays stored recordings (free, but fails on any
# prompt it has never seen).
BUCKER_MODEL_MODE=live uv run python -m bucker.worker &

# create a task and watch it run
uv run python -m bucker.cli start --objective "my first durable task" --wait
uv run python -m bucker.cli events <task_id>   # the full audit trail
uv run python -m bucker.cli show    <task_id>   # state, rebuilt from events
```

### Windows notes (native)

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
   uv sync --extra dev --extra llm
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

The smoke run needs a model. Two free options:

### Option A — Ollama (local, recommended to start)

The model runs on your machine. No account, no key, no cost.

```bash
ollama pull qwen2.5-coder:7b
```

Then in `.env`:

```
BUCKER_MODEL=ollama/qwen2.5-coder:7b
BUCKER_MAX_TOKENS_PLANNER=1000
BUCKER_MAX_TOKENS_WORKER=3000
```

The 7B model needs roughly 8 GB of free memory. On a smaller machine, pull
a smaller coding model (`qwen2.5-coder:3b`) and point `BUCKER_MODEL` at it.
The smoke-run preflight checks that Ollama and the model are ready before it
creates a task.

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

### Option B — OpenRouter (free tier or paid)

OpenRouter's free tier works for a first run. Create a key at
openrouter.ai, then in `.env`:

```
OPENROUTER_API_KEY=sk-or-v1-...
BUCKER_MODEL=openrouter/nvidia/nemotron-3-super-120b-a12b:free
BUCKER_MAX_TOKENS_PLANNER=1000
BUCKER_MAX_TOKENS_WORKER=3000
```

Free-model availability and rate limits change; pick a current `:free`
model from [OpenRouter's list](https://openrouter.ai/models) if needed.
The token ceilings are hard stops — an unbounded generation is an unbounded
bill.

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

- **Web dashboard** — `uv run uvicorn bucker.api.app:app --port 8000`
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
bucker setup           # wizard: proposes a free-first chain (dry run)
bucker setup --apply   # ...and writes it into .env (other lines untouched)
```

The wizard proposes a deterministic free-first chain — best local coder →
best free hosted → best paid — and `BUCKER_MODEL_FALLBACKS` makes the
router fall through it when a provider fails (dead key, quota, outage).
Replay determinism is unaffected: replays stay keyed to the primary model,
and the router never silently reorders the chain.

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
runs the full plan → work → verify loop as a child workflow. Schedules
live in Temporal, so they survive restarts. Manage them from the
dashboard at `/schedules-page`.

## Task templates

The new-task form (and schedules) start from named presets — code-fix,
feature-add, research, data-extraction, demo — each with a sensible
objective, budget and deadline. `bucker templates` lists them; one click
on the form fills everything in.

## CLI

```bash
bucker start --code --objective "..." --budget-usd 0.5 --wait
bucker tasks                # recent tasks: status, cost, tokens
bucker usage                # tokens/cost by model and stage
bucker show <id>            # state rebuilt from events
bucker events <id>          # the full audit trail
bucker replay <id>          # deterministic re-run from recordings
bucker templates            # task presets
bucker schedules list       # recurring tasks
bucker doctor               # diagnose a broken setup
```

## Web UI + HTTP API

The dashboard is server-rendered HTML — no frontend build step.

```bash
uv run uvicorn bucker.api.app:app --port 8000
open http://localhost:8000/          # aggregate dashboard
open http://localhost:8000/tasks/new # create a task from the browser
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
curl -X POST "http://localhost:8000/tasks?objective=Add%20a%20subtract%20function%20to%20calc.py&budget_usd=0.50&deadline_minutes=10"

# list tasks with cost + event counts; filter by status
curl "http://localhost:8000/tasks?status=completed"

# the audit trail, and a deterministic replay from recordings
curl "http://localhost:8000/tasks/<id>/events"
curl -X POST "http://localhost:8000/tasks/<id>/replay"

# control: re-run a finished task (new task, same objective), cancel a running one
curl -X POST "http://localhost:8000/tasks/<id>/rerun"
curl -X POST "http://localhost:8000/tasks/<id>/cancel"

# system health as JSON
curl "http://localhost:8000/api/system"
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
curl -X POST "http://localhost:8000/tasks?objective=...&adaptive=true"
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

The meta-rule is simple: **generated code is untrusted worker output, and
the test suite is its verifier.** Nothing merges without passing, no matter
who wrote it — that is the same principle the platform applies to its own
agents, applied to its own construction.

## License

Apache-2.0. See [LICENSE](LICENSE).
