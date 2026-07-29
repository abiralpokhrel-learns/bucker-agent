# Building the Durable Execution & Evaluation Platform — Solo Builder's Guide

**For:** one person, near full-time (~25–30 focused hrs/week), Python, shipping open source.
**Source:** your 24-document package (July 2026), re-scoped from its 2–4-engineer/12-month plan to a realistic solo path.
**Stack:** Python 3.12 · Temporal (OSS, local dev server) · Postgres · Docker · SWE-bench Lite.

**The one rule:** your own docs' best idea is the go/no-go gate. Everything below is organized so that by ~Week 12 you have run the benchmark and have *evidence* — and you decide from evidence, not sunk cost. Do not reorder phases, and do not build anything on the cut list.

---

## Phase map (solo timeline)

| Phase | Doc plan | Solo weeks | Deliverable | Gate (hard checkpoint) |
|---|---|---|---|---|
| 0 — Foundation | Months 1–2 | **Weeks 1–3** | Event-sourced state + durable workflow | M1: kill -9 mid-task → resume, zero data loss |
| 1 — Single domain | Months 2–4 | **Weeks 4–12** | Planner→Worker→Verifier for code + benchmark harness | M2: **go/no-go** — beat single-agent baseline on same model, or stop and rethink |
| 2 — Scheduling & observability | Months 4–6 | **Weeks 13–17** | Budget/deadline enforcement, telemetry dashboard, adaptive retry | M3: adaptive planner measurably cuts repeat-failure rate |
| 3 — Second domain & eval pipeline | Months 6–9 | **Weeks 18–26** | Second verifier + candidate→benchmark→promote→rollback pipeline | M4: promotion with rollback demonstrated end-to-end |
| 4 — Research track | Month 9+ | **Cut for solo** | — | — |

---

## Week 0 — Prerequisites (do this before Phase 0)

**Skills check.** You need working comfort with: Python `asyncio`, Docker (build/run/volumes), SQL basics, and git. Temporal you can learn as you go — its free "Temporal 102 (Python)" course covers workflows vs. activities, determinism, and replay; do it in week 0, it maps 1:1 onto this project.

**Machine.** 16 GB RAM minimum, and **100+ GB free disk** — SWE-bench evaluation images are large; the harness's `--cache_level=base` flag reduces storage. If your laptop is weak, plan to run benchmark evaluation on a cheap cloud VM instead (the harness also supports Modal / `sb-cli` cloud runs).

**Setup checklist:**

```bash
# 1. Temporal CLI + local dev server (has built-in UI at localhost:8233)
curl -sSf https://temporal.download/cli.sh | sh
temporal server start-dev          # leave running in a terminal

# 2. Project scaffold
mkdir agentplatform && cd agentplatform && git init
uv init --python 3.12              # or poetry
uv add temporalio asyncpg pydantic jsonschema litellm fastapi uvicorn pytest

# 3. Postgres (YOUR event log — separate from Temporal's internal store)
docker run -d --name platform-pg -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:16
```

**Repo structure to grow into:**

```
agentplatform/
  core/            # event store, state reconstruction, snapshots
  contracts/       # JSON Schemas for Task, results, events (versioned)
  workflows/       # Temporal workflow definitions
  activities/      # planner, worker, verifier, tool-runtime activities
  verifiers/       # plugin interface + python_test_runner
  router/          # model-router (model name = config, never hardcoded)
  replay/          # deterministic replay engine
  bench/           # benchmark harness + baseline single-agent loop
  api/             # FastAPI: POST /tasks, GET /tasks/{id}, /events, /replay
  blob/            # local object storage for large tool outputs (S3 later)
  docs/decisions.md
```

**Open-source from day 1:** public GitHub repo, Apache-2.0, README containing the one-liner ("Nothing is trusted until it's verified, nothing is lost when it crashes, nothing overspends silently, nothing changes in production until it's proven better"), and a `docs/decisions.md` you append to weekly. Building in public *is* the go-to-market plan from doc 24, started early.

**Two layers of durability — understand this distinction before writing code.** Temporal makes your *workflow execution* durable (retries, timeouts, resume after crash). Your *Postgres event log* is the product's source of truth (FR1–FR2) — the thing users query, replay, and audit. Temporal is plumbing you rent; the event log + contracts + verifiers + harness are the product you're building.

---

## Phase 0 — Weeks 1–3: the durable core

**Goal:** a dummy multi-step task that survives `kill -9` and resumes with zero data loss and no duplicated side effects. Nothing AI yet. This is deliberately boring — it's the foundation everything else trusts.

### Week 1 — Event log (FR1, FR2)

1. Write the schema exactly as in your Database Design Document:

```sql
CREATE TABLE events (
  id              BIGSERIAL PRIMARY KEY,        -- monotonic ordering
  task_id         UUID NOT NULL,
  event_type      TEXT NOT NULL,                -- TaskCreated, PlanGenerated, ...
  payload         JSONB NOT NULL,
  schema_version  INT  NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  tool_output_ref TEXT                          -- pointer into blob storage
);
CREATE INDEX ON events (task_id, id);

CREATE TABLE tasks (
  id UUID PRIMARY KEY, parent_id UUID, task_type TEXT, status TEXT,
  budget NUMERIC, deadline TIMESTAMPTZ, verifier TEXT, current_snapshot JSONB
);

CREATE TABLE snapshots (
  task_id UUID, version INT, state JSONB, created_at TIMESTAMPTZ,
  PRIMARY KEY (task_id, version)
);

-- Append-only at the DB level (doc 13's constraint, enforced, not promised):
CREATE ROLE app_rw LOGIN PASSWORD 'dev';
GRANT INSERT, SELECT ON events TO app_rw;   -- no UPDATE, no DELETE
```

2. `core/eventstore.py`: `append(task_id, event_type, payload)`, `read_stream(task_id)`, `rebuild_state(task_id)` (fold events → state dict), `snapshot_every_n(50)`.
3. Unit tests: write/read correctness; property test that `rebuild_state(events) == rebuild_state(snapshot + tail)`.

### Week 2 — First durable workflow

1. One Temporal workflow (`workflows/task_workflow.py`) with 5 fake steps; each step is an *activity* that (a) does a fake side effect, (b) appends an event. Workflow code must be deterministic — all I/O in activities (this is the Temporal 102 material).
2. **Idempotency now, not later:** every activity writes with an idempotency key (`task_id + step_n`) so a retried activity can't double-append. This is what makes "no duplicate side effects" true later when side effects are real git pushes.

### Week 3 — Crash-and-resume proof (M1)

1. Script `tests/crash_test.sh`: start a task → `kill -9` the worker process between steps 3 and 4 → restart worker → assert task completes, event stream has no gaps/duplicates, `rebuild_state` matches expected.
2. Make it a CI job (GitHub Actions, Postgres + Temporal dev server as services).
3. Measure and record your doc's performance targets: event write < 50 ms p95, state reconstruction < 500 ms at 1,000 events, resume < 5 s.

**Definition of done / M1:** crash test green in CI, targets recorded in README. Tag `v0.1.0`. Write build-log post #1 ("I killed my agent 50 times and it didn't lose a byte") — that's your first credibility artifact.

---

## Phase 1 — Weeks 4–12: Planner → Worker → Verifier + the benchmark

**Goal:** the go/no-go evidence. One domain (code), one worker, real verification, record-replay, and a fair benchmark against a single-agent baseline on the same model.

### Week 4 — Typed Task contract (FR4)

1. `contracts/task.schema.json`, exactly the doc's shape:

```json
{
  "task_type": "code_change",
  "objective": "Add JWT authentication",
  "files": ["auth.py", "middleware.py"],
  "constraints": {"tests_required": true, "coverage": 90},
  "budget": 0.75,
  "deadline_minutes": 15,
  "verifier": "python_test_runner"
}
```

2. Every contract carries `schema_version`. Validation failures are logged as `SchemaValidationFailed` events (never silently dropped) and trigger one re-prompt, then fail the task.
3. `router/`: thin wrapper over LiteLLM; model name comes from config/env only. This is the "LLM is a replaceable plugin" mechanism — enforce it with a lint rule if you have to.

### Weeks 5–6 — Planner and sandboxed tool runtime

1. **Planner activity:** input = objective + current state from the event log; output = typed Task (or small Task graph). Validate → `PlanGenerated` event. Store the *full raw model response* verbatim (blob storage, `tool_output_ref`) — this is what makes replay possible.
2. **Tool runtime:** every worker tool call (run tests, apply diff, read file) executes inside a per-task Docker container. No host access, no network by default. Stdout/stderr/exit codes stored verbatim. Secret-scan outputs before storage (doc 19); a simple regex pass for key patterns is fine for v1.
3. Start blob storage as content-addressed files in `blob/` (sha256 filename). Swap for S3/MinIO later without changing the event schema — `tool_output_ref` already abstracts it.

### Week 7 — Worker + verifier plugin

1. **Worker activity:** takes one typed Task, produces a structured result (file diff + status), schema-validated before it touches state (`WorkerCompleted`).
2. **Verifier interface** (`verifiers/base.py`): `verify(task, result, sandbox) -> {passed, diagnostics}`. Register `python_test_runner`: apply diff in sandbox → run pytest (+ optionally ruff) → pass/fail + failing test names. Events: `VerificationRequested`, `VerificationPassed` / `VerificationFailed`.
3. **Retry policy:** `VerificationFailed` → `RetryScheduled` with failure context fed back to the planner, up to `max_retries` (start: 2) → then `NeedsHumanReview`. Fixed policy only — adaptive strategies are Phase 2; resist the urge.

### Week 8 — Deterministic replay (FR5)

1. `replay/engine.py`: re-run a task's workflow where every model/tool call is answered from stored outputs instead of live calls (your SRS constraint: determinism by record-replay, never by re-invoking the model). Output: match/mismatch vs. original verification outcome.
2. `POST /tasks/{id}/replay` + integration test: run task live → replay → assert identical outcome.
3. Dev mode runs entirely on mocked/stored LLM responses — from here on, most of your iteration burns zero API dollars. This matters for a solo budget.

### Weeks 9–11 — Benchmark harness (FR8)

1. **Baseline:** a deliberately simple single-agent loop — same model, same sandbox, same tools, no planner/verifier structure. Princeton's mini-SWE-agent (~100 lines) is the honest reference design; implement or adapt the equivalent.
2. **Task set:** SWE-bench Lite (the subset its authors recommend starting with). Iterate on **25–50 instances**; the official harness runs patches in Docker (`swebench.harness.run_evaluation`, `--max_workers 8`, watch disk).
3. **Harness:** run identical instances through (a) your platform, (b) baseline. Per run, from your own telemetry: success rate, cost/task, latency, recovery rate. Persist to the experiment-log format from doc 16 (run_id, architecture, model, task set, metrics).
4. **Stats honesty:** paired comparison on identical instances; report McNemar's test or a bootstrap CI, not a bare delta. On 25–50 instances only a large gap is meaningful — say so in the README rather than overclaiming ("vibes-based claims" is the sin your market-research doc pins on everyone else).
5. **Budget:** iterate with a cheap model; do the headline comparison once with a frontier model. Expect a full paired 300-instance Lite comparison with a frontier model to cost real money (low hundreds of USD); a 50-instance paired run is typically tens. Your own cost telemetry is the meter — dogfood it.

### Week 12 — The gate (M2)

Run the full comparison. Then the decision rule from your ML Experiment Plan, verbatim: proceed to Phase 2 **only if** the platform shows a statistically meaningful improvement in success rate or a clearly favorable cost/success tradeoff. Otherwise: revisit architecture before adding complexity — shrink task granularity, fix the planner prompt, re-gate. **Publish the result either way** (methodology + harness + numbers). A rigorous negative result still builds your reputation and still leaves you owning a benchmark harness people want; only silence is a loss.

This is also your open-source launch moment: quickstart-in-10-minutes README, the benchmark post, "reproduce it yourself" instructions, HN/r/MachineLearning/Temporal community.

---

## Phase 2 — Weeks 13–17: scheduling & observability

Only reached if M2 passed.

1. **Budget/deadline enforcement (UC-6):** scheduler checks cumulative cost/time per task (you already have per-event cost telemetry); on breach → halt + `BudgetExceeded`/`DeadlineExceeded` event. Test under concurrent load, not just isolation (doc 20).
2. **Telemetry dashboard:** one FastAPI page per task answering doc 08's UX requirement in a single view — what happened, why, how long, how much, replay link. Aggregate view: success rate and cost by task type over time. Plain HTML tables are fine; Grafana-on-Postgres is fine. No React project.
3. **Adaptive planning:** on repeated failure, vary strategy — switch model / chunk the task smaller / ask for clarification. A/B it against fixed retry on a failure-prone task set (injected flaky tests, ambiguous objectives). **M3:** measurable reduction in repeat-failure rate.

## Phase 3 — Weeks 18–26: second domain & the promotion pipeline

1. **Second verifier** to prove the plugin interface generalizes: research/citation-consistency checking (doc's suggestion) or a docs-consistency verifier — pick per what early users ask for. Expect it to be *harder* than code (subjective ground truth — your Risk Assessment flags reward-gaming here; keep a human-review step).
2. **Offline evaluation pipeline (UC-5):** `candidates` table → Evaluation scores recent outcomes → you propose a strategy change → Benchmark Runner tests it vs. current baseline → human approval (you, recorded) → Promotion with `rolled_back` path.
3. **Replay-based regression suite:** before promoting any candidate, replay a fixed suite of past workflows; any previously-passing task that now fails blocks promotion automatically (doc 20/22). **M4:** one full promote→regress→rollback cycle demonstrated in CI.

---

## The cut list (equally important)

Explicitly do **not** build, per your own Non-Goals plus solo reality: multi-worker parallel execution; universal reward functions or cross-domain skill generation; autonomous self-modification; a hosted SaaS/managed offering; a polished web UI; auth/multi-tenancy beyond a bearer token; Kubernetes (docker compose until real users complain); custom scheduling theory (cron + priority ints); fine-tuning anything.

## Weekly operating rhythm

Mon–Thu: build (one phase-task per day-ish). Fri morning: crash test + replay spot-check still green (your docs call for periodic replay spot-checks — automate as a weekly CI cron). Fri afternoon: append to `decisions.md`, push a short public changelog. Every phase gate: stop, write the honest status post, decide.

## Kill / pivot criteria (decide in advance, in writing)

- End of Week 3: M1 not demonstrable → you've learned the real cost of the foundation; re-estimate everything before continuing.
- End of Week 12: no working end-to-end benchmark at all → scope was too big; descope to "durable verified coding-task runner" (drop planner, keep worker+verifier) and re-gate in 3 weeks.
- M2 clearly negative after one architecture revision → publish, then pivot to the asset with independent value: the **benchmark harness for comparing agent architectures under identical conditions** — the crowded observability market has tracing tools, not architecture-comparison harnesses.
- Any point: two consecutive weeks with zero commits → the timebox, not the vision, is the problem; shrink the weekly goal.

**Worst case, fully realized:** ~3 months, a few hundred dollars of API spend, and you exit with demonstrated Temporal + event-sourcing + LLM-evaluation skills, a public benchmark artifact, and a rigorous public write-up. That failure mode is a strong portfolio.
