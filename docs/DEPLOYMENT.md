# Deployment

bucker-agent is **prototype (pre-1.0)**: the durable core and the
plan→work→verify pipeline are demonstrated; the benchmark gate, promotion
pipeline, and replay regression gate have no published live numbers yet. Run
it for evaluation and iteration today; treat it as production infrastructure
only after the M2 gate produces evidence. This document is the checklist for
either case.

## The five things to change before exposing it

The quickstart is tuned for one developer on a laptop. Every item below is
safe to skip locally and unsafe to skip when the API faces real traffic.

### 1. The database role (dev bootstrap vs production)

`migrations/001_init.sql` creates the `bucker_app` role with password `dev`
so a fresh clone works with zero setup. That block only runs when the role
does not exist — it never overwrites existing credentials. For production:

```sql
CREATE ROLE bucker_app LOGIN PASSWORD '<strong-random-password>';
```

then point `BUCKER_DATABASE_URL=postgresql://bucker_app:<that-password>@host:5432/bucker`
in your secrets store. The append-only guarantee is enforced at the
permission level: `bucker_app` has `INSERT, SELECT` on `events` and the
migration `REVOKE`s `UPDATE, DELETE, TRUNCATE` — verify that survives your
DDL process.

### 2. The API token

`BUCKER_API_TOKEN=dev-token` bypasses auth entirely (for localhost only —
the app refuses non-localhost Host headers while the token is the dev
default, and prints a warning at startup). Set a real token:

```
BUCKER_API_TOKEN=<random-token>
```

and send it as `Authorization: Bearer <token>` on every request. The
dashboard, API, and replay endpoints all check it.

### 3. Model keys

Provider keys live in `.env` (git-ignored). Exactly one key per provider,
never committed, never pasted into issues or chat. The system page reports
key *shape* only — if you see a key value anywhere in the UI, that is a bug.

### 4. Token ceilings and budgets

`BUCKER_MAX_TOKENS_*` are hard stops (an unbounded generation is an
unbounded bill). `BUCKER_DEFAULT_BUDGET_USD` and per-task `budget_usd` feed
the workflow's policy; the workflow accumulates real model costs from the
planner and worker activities and halts when `cost_usd > budget_usd`.

The halt is *pre-spend*: before each model call the workflow checks
`cost_so_far + step_estimate_usd` (a conservative per-call reserve,
default $0.02) against the budget, so a single expensive call cannot
overshoot a tight budget unchecked. Note the ordering: the budget comes
from the planner's contract unless you pass one in, so **the planner call
itself is not budget-guarded** — the guard applies to everything after
planning. For a hard ceiling from the first token, always pass
`budget_usd` explicitly.

### 5. Network-isolated sandbox

Containers run with `--network none` by default. If you enable network for
a task, you have removed the primary containment boundary — the verifier,
not the sandbox, is then your only check on model output.

## Running it

```bash
uv sync --extra dev --extra llm
uv run python -m bucker.cli migrate          # as the OWNER role (DDL rights)
uv run python -m scripts.doctor              # diagnose a broken setup, fails cleanly
BUCKER_MODEL_MODE=live uv run python -m bucker.worker &
uv run uvicorn bucker.api.app:app --host 127.0.0.1 --port 8000
```

`BUCKER_MODEL_MODE=live` makes the worker call the real provider; the
default `recorded` mode replays stored responses and fails on any prompt it
has never seen.

## Observability

- `/system` — model chain, provider reachability, Postgres/Docker/Temporal/
  sandbox-image health, verifier registry.
- `/usage` — tokens and cost by model, by pipeline stage, per day.
- `/tasks/{id}/dashboard` — full event timeline plus the complete verifier
  diagnostics (the exact text a retry would feed back).
- `/tasks/{id}/replay` — deterministic re-run from recordings; replays run
  in an isolated workspace copy and never mutate the original.

## Backups

The durable state is the Postgres `events` table (append-only) plus the blob
store (`BUCKER_BLOB_ROOT`, content-addressed model traffic). Back them up
together: recordings reference blobs by content hash, and a blob store
without its events (or vice versa) leaves dangling refs. The `workspace/`
directory is derived data — regenerate or lose it freely.
