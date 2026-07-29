# bucker-agent

**Nothing is trusted until it's verified, nothing is lost when it crashes, nothing overspends silently, and nothing changes in production until it's proven better.**

A durable execution and evaluation platform for AI agents. Not another agent framework — the layer *underneath* one. Bring your own agent loop (Hermes-style, OpenClaw-style, your own); bucker makes it crash-proof, verification-gated, cost-bounded, and benchmarkable.

The LLM is the replaceable part. Swap it for a stronger model next year and the platform works the same way. The durability, verification, and evidence-based improvement is what the system actually *is*.

> **Status: Phase 0.** The durable core is real and tested. Planner/worker/verifier land in Phase 1. See [`BUILD_PLAN.md`](BUILD_PLAN.md) for the 40-step roadmap.

---

## How it works

1. **Everything is an event, and the event log is the truth.** No "current state" is stored as the primary thing — state is a replay of history. Crashes aren't catastrophic: the system re-derives where it was and picks up. The `events` table is append-only *at the database permission level*, not by convention.
2. **A Planner turns a fuzzy goal into a strict, typed contract** (`task_type`, `objective`, `constraints`, `budget`, `deadline`, `verifier`). Validation failures are recorded as events, never silently dropped.
3. **A Worker executes — and its output is never trusted on its own.**
4. **A domain-specific Verifier checks the result.** Code gets tested and linted. There is no universal "is this good?" function; each domain plugs in its own objective check. Repeated failure escalates to a human instead of being forced through.
5. **Every step is logged with cost, time, and outcome, and can be replayed exactly.** Determinism comes from record-and-replay of stored model/tool outputs — never by re-invoking the model.
6. **Improvements are proposed, benchmarked, and only promoted if they win.** No live self-modification. Human approval, rollback retained.

## Quickstart

Requires Python 3.12+, Docker, and the [Temporal CLI](https://docs.temporal.io/cli).

```bash
git clone <your-repo-url> && cd bucker-agent
cp .env.example .env

docker compose up -d                 # Postgres
temporal server start-dev            # UI at http://localhost:8233

uv sync --extra dev
uv run python -m bucker.cli migrate  # apply schema + append-only grants
uv run python -m bucker.worker &     # start the worker

uv run python -m bucker.cli start --objective "my first durable task" --wait
uv run python -m bucker.cli events <task_id>   # the full audit trail
uv run python -m bucker.cli show   <task_id>   # state, rebuilt from events
```

### Prove the durability claim yourself

```bash
uv run python -m tests.crash_test
```

Starts a task, hard-kills the worker (`os._exit`) between a side effect and its event append — the nastiest possible window — restarts, and asserts the task completes with every step recorded exactly once, the side effect performed exactly once, and reconstructed state matching a full replay. Exit code 0 means **M1** is demonstrated.

## Testing

```bash
uv run pytest                                  # pure tests, no infra needed
BUCKER_TEST_DATABASE_URL=postgresql://postgres:dev@localhost:5432/bucker uv run pytest
```

Database tests skip automatically when that variable is unset, so a fresh clone tests green with nothing running.

## Project layout

```
bucker/
  core/          event store, state fold, snapshots, blob storage   [HAND]
  contracts/     typed Task contract — JSON Schema + pydantic       [HAND]
  workflows/     Temporal workflow definitions (deterministic!)     [HAND]
  activities/    all side effects live here, idempotent by key      [HAND]
  router/        model router — model name is config, never code    (Phase 1)
  verifiers/     verifier plugin interface + implementations        (Phase 1)
  replay/        deterministic record-replay engine                 (Phase 1)
  bench/         baseline agent + paired benchmark harness          (Phase 1)
  api/           FastAPI surface                                    (Phase 1)
tests/
  crash_test.py  the M1 durability proof
migrations/      SQL, append-only grants included
```

## The vibe-code rule

Files marked `[HAND]` in their module docstring are the ones where a subtle bug silently poisons everything above them: the event store, the state fold, idempotency, replay, verifiers, and the stats module. Read every line of those.

Everything else — configs, CI, dashboards, plumbing — is fine to generate.

The meta-rule: **generated code is untrusted worker output, and the test suite is its verifier.** Nothing merges without passing, no matter who wrote it. That is the same principle the platform applies to its own agents, applied to its own construction.

## Roadmap

| Phase | Weeks | Gate |
|---|---|---|
| 0 — Durable core | 1–3 | **M1:** `kill -9` → resume, zero data loss ✅ scaffolded |
| 1 — Plan→Work→Verify + benchmark | 4–12 | **M2 (go/no-go):** beat a single-agent baseline on the same model, or stop and rethink |
| 2 — Scheduling & observability | 13–17 | **M3:** adaptive planning measurably cuts repeat-failure rate |
| 3 — Second domain & promotion pipeline | 18–26 | **M4:** promote → regress → rollback proven end to end |

M2 is a real gate with a real kill switch. The benchmark result gets published either way — a rigorous negative result is still a contribution, and the credibility gap in this space is exactly that nobody publishes reproducible comparisons.

## License

Apache-2.0.
