# Changelog

All notable changes to bucker-agent are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **One-command onboarding** — `bucker dev` is now THE command: first run
  auto-bootstraps (prereq checks → uv auto-install prompt → `.env` + token
  → Postgres → migrations) then starts the whole stack and opens the
  dashboard in your browser; later runs just start the stack. First-run
  detection is real (no `.env` / DB down / unapplied migrations →
  bootstrap). New flags: `--force-setup`, `--no-browser`; `--dry-run`
  reports whether setup is needed. Platform launchers `start.sh` /
  `start.bat` install uv if missing; `Makefile` (`make dev`); `.env`
  generation and Docker prompts now offer to act (install uv / open the
  Docker download page) instead of only printing instructions.

### Fixed

- **CI sandbox image** — GitHub Actions never built `bucker-sandbox:latest`;
  tests that docker-run real sandbox containers failed with "Unable to find
  image", which then cascaded into `schedule_failed` transitions. Both CI
  jobs (test + crash-resume) now build the image from `Dockerfile.sandbox`
  before running.
- **`GatewayModel.__dict__` crash** — `_replace()` used `model.__dict__`
  on a slots dataclass, raising AttributeError on every
  `ModelRegistry.default()` in CI (where the default model id is not in
  the catalog). Now uses `dataclasses.asdict`; regression test simulates
  the no-`.env` chain.
- **`tasks_status_check` rejected `schedule_failed`** — the scheduler and
  API write it (`api/app.py`, `core/tasks.py`), but migrations 001/003 did
  not allow it, so the writes violated the CHECK on a real database
  (exposed in CI). Migration 005 adds it (idempotent drop-then-add);
  tripwire + real-DB tests. The test DB fixture now truncates BEFORE
  migrations so constraint re-runs never see stale rows.

### Added

- **ModelRouter-v2 bridge** — the internal inference path (planner /
  worker / critic) now runs through the gateway engine in live mode:
  `ModelRouter.complete()` stays the stable API (no call sites changed),
  recorded mode is untouched (deterministic replay), and live mode
  delegates to `RouterEngine` — gaining capability filtering, policy
  routing, circuit breakers, and fallback. The recording carries a
  **routing envelope** (policy, registry `config_version`, candidates,
  selected provider/model, reason, fallback attempts) so replay is a pure
  lookup and never re-decides routing: *live = intelligent routing,
  replay = historical reconstruction*. Regression-proof: a hard test
  proves same-digest replay returns the identical response while the
  engine is never contacted, even with provider health flipped. The
  empty-content guard (reasoning models burning the output budget) moved
  from the router into the engine, covering every adapter. Adaptive
  `next_model` now resolves through the model registry instead of a
  hardcoded `FALLBACK_MODELS` list (which silently named paid OpenRouter
  models, violating the free-only rule) — adaptive expresses a
  requirement, the gateway picks the deployment. `ModelResponse` gained
  `tool_calls`/`finish_reason` (Phase 4 tool-calling worker surface).
- **Inference gateway engine** (`bucker/gateway/`) — the OpenAI-compatible
  `/v1` surface is now a policy-driven inference gateway, not a
  passthrough: canonical request/response model, capability model registry
  (tools/streaming/vision/reasoning/context/pricing/free-tier), provider
  adapters (DeepSeek, OpenRouter, Ollama; OpenAI-compatible base class +
  scripted `SimulatedProvider` for hermetic tests), routing engine with
  six policies (priority/cost/latency/balanced/free-only/local-first),
  deadline-bounded retries with backoff + jitter, circuit breakers,
  Postgres-backed quota ledger (`gateway_usage`, migration 004),
  normalized error taxonomy, SSE streaming with tool-call deltas, and
  `/health/live` + `/health/ready` endpoints. Requests that fail their
  hard requirements are rejected before any provider is called.
- **Self-critique loop (loop engineering)** — every produced diff is
  reviewed by a critic pass before it costs a sandbox verification cycle;
  verdicts other than *ok* trigger one bounded repair round
  (`BUCKER_ENABLE_CRITIQUE`, `BUCKER_MAX_CRITIQUE_ROUNDS`). Critic and
  repair failures never sink the task — they degrade to no-critique.
- **Graph engineering** — multi-step task DAGs: independent steps run as
  parallel Temporal child workflows per wave, steps join on their
  dependencies, strict DAG validation (cycles/duplicates/unknown deps).
  CLI `bucker graph run`, API `POST /graphs`, `examples/graph_demo.json`.
- **Human-in-the-loop approval gate** — escalated
  (`needs_human_review`) tasks can be approved or rejected with a note
  (`POST /tasks/{id}/approve|reject`); the verdict is append-only and the
  task becomes `human_approved`/`human_rejected`, distinct from machine
  verdicts. Dashboard buttons included.
- **Free-tier quota panel** — catalogued free models carry documented
  daily limits; `/usage` shows today's per-model calls vs the cap with
  remaining estimates (OmniRoute-style visibility).
- **Self-curating memory** — finished tasks are automatically
  consolidated into semantic-memory facts (`BUCKER_AUTO_CONSOLIDATE`);
  `bucker memory status` audits the store and `bucker memory prune`
  dedupes identical facts and caps it.
- **Result delivery (gateway)** — scheduled and graph runs announce
  their outcome to a webhook or Telegram (`BUCKER_NOTIFY_WEBHOOK_URL`,
  `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`); opt-in, never raises.
- **Sandbox start-retry** — transient `docker run` failures are retried
  (3 attempts, bounded backoff) instead of killing the task.
- **Task dashboard loop panels** — self-critique verdicts and graph step
  statuses surfaced on the task page.

### Fixed

- `plan_task`/`run_worker` activity annotations now match their
  `(dict, cost)` tuple returns — Temporal decodes activity results by the
  annotation, so live execution since the budget-tuple change would have
  failed with "Expected dict, value was list".
- Generic webhook delivery built an absolute-URI request line — now
  origin-form via `urlparse`.
- Memory prune ordering is deterministic even under Windows' ~15.6ms
  clock tick (strictly increasing fact timestamps).
- `tasks.status` was a stale cache (folded "pending") — the terminal
  policy activity now keeps it honest, and `scripts/backfill_status.py`
  replays the event stream to sync historical rows.
- **Human-review statuses were illegal in the DB schema** — the original
  `tasks_status_check` did not allow `human_approved`/`human_rejected`,
  so the approval gate's UPDATE would have failed on a real database
  (API tests used a fake connection and missed it). `migrations/003`
  re-declares the constraint; live-verified: approve on a real DB flips
  the row to `human_approved`, bogus statuses are still rejected
  (`CheckViolationError`). Migration tripwire tests added.
- API tests now simulate "Temporal down" hermetically (patch
  `Client.connect`) instead of relying on ambient infra state.
- **Review pass (readiness)**: `scripts/backup.py` (Postgres + blobstore,
  timestamped, retention) with a validated restore drill;
  `docs/OPERATIONS.md` (backup/restore, monitoring, log retention,
  migration rollback policy, incident runbook, M2 procedure, pre-publish
  checklist); doctor warns when `BUCKER_API_TOKEN` is the dev default;
  Docker images pinned by digest (`postgres:16`, `python:3.12-slim`);
  placeholder URLs replaced with the real repository
  (github.com/abiralpokhrel-learns/bucker-agent);
  M1 crash-resume gate re-run live (exit 0).

- **Hardening review — enforced behavior, not documentation**:
  production mode refuses to boot with the dev-token default (API +
  worker, exit 2); scheduling failures are visible (ScheduleFailed event,
  `schedule_failed` status, error in the API response) with a
  `bucker reconcile` re-scheduler; read-only token tier with admin-only
  mutations; memory/skills API writes gated; unknown model cost is NULL
  in telemetry and halts budgeted workflows (fail closed); archived
  prompts/provider responses redacted for credentials; webhook delivery
  refuses private/SSRF targets; migration-upgrade tests against real
  Postgres; `scripts/restore_drill.py` (PASSED live, 947 rows);
  doctor diagnoses the uv Python-install trap.

- **Makeover pass — gateway + Docker path + dashboard polish**:
  `POST /v1/chat/completions` + `GET /v1/models` (OpenAI-compatible
  gateway, like OmniRoute): your BUCKER_API_TOKEN is the API key, the
  free-first chain routes with auto-fallback, every call is audited as
  a task with cost (live-proven: real DeepSeek call → OpenAI-shaped
  response, $0.000026); `Dockerfile` + full-stack compose (postgres +
  temporalio/auto-setup + api + worker, `.env` optional) so a fresh
  machine needs ONLY Docker; `bucker dev`/`setup` from the usability
  pass; dashboard CSS polish (sticky gradient header, hover states,
  focus rings).
- **One-command setup (usability pass)** — new `bucker setup` (checks
  prerequisites, generates `.env` + a real API token, starts Postgres,
  migrates — one command) and `bucker dev` (starts Temporal + worker +
  dashboard in ONE terminal; detects what is already running and skips
  it, falls back to the Temporal docker image when the CLI is missing).
  The old model-key wizard moved to `bucker setup-wizard`. Setup is now:
  install Docker + uv → `uv sync` → `bucker setup` → `bucker dev`.
- **Model fallback chain** — `BUCKER_MODEL_FALLBACKS` (comma-separated) is
  tried in order when the primary model fails (provider down, key rejected,
  quota exhausted). Recordings keep both the configured primary and the
  model that actually served, so replay determinism is unaffected.
- **Control dashboard** — new `/system` control-center page (model chain,
  provider reachability, Postgres/Docker/Temporal/sandbox-image health,
  verifier registry, recordings and task counts) and a `/api/system` JSON
  endpoint. Provider checks report key *shape* only, never the value.
- **Task control actions** — `POST /tasks/{id}/rerun` (new task with the
  same objective; the original event stream is never mutated) and
  `POST /tasks/{id}/cancel` (terminates the Temporal workflow), surfaced as
  buttons on the task dashboard.
- **Open-source packaging** — `SECURITY.md` (trust model + reporting),
  `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `CHANGELOG.md`,
  `[project.urls]` metadata in pyproject.toml.
- **README rewrite** — simpler instructions, verified smoke-run results
  table, and a documented model-choice section (Ollama vs OpenRouter).

### Fixed

- **Diff apply tolerance** — `apply_diff` now repairs model-sloppy diffs
  before applying: missing `---`/`+++` headers are inferred from the
  worker's `files_touched`, missing `+` prefixes are restored, hunk line
  counts are inferred with `git apply --recount`, and the `patch` fallback
  chain tries `-p1` then `-p0`.
- **Workspace line endings** — `scripts/smoke_run.py` writes workspaces
  with `newline=""`; Windows `Path.write_text` was injecting CRLF into the
  Linux sandbox, breaking every LF-context diff.
- **JSON repair** — `planner.extract_json` repairs small-model JSON tics
  (unescaped interior quotes, forgotten string-close quotes, missing
  commas), with a byte-exact test suite.

### Verified

- Live smoke run (`ollama/qwen2.5-coder:7b`) and its recorded replay both
  PASS (verifier: 3 tests passed; replay in 3.1 s, deterministic).

## [0.1.0] - 2026-07

- All 40 BUILD_PLAN steps implemented: durable event-sourced core (M1
  crash-test proof), plan→work→verify pipeline, Temporal + Postgres
  integration, Docker sandbox with `--network none`, record-and-replay
  router, benchmark/stats/promotion tooling (M2–M4 scaffolding), and the
  FastAPI + dashboard surface.
