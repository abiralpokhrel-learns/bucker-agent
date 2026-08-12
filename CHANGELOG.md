# Changelog

All notable changes to bucker-agent are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Lite path is now genuinely light** — `temporalio` and `asyncpg` moved
  out of the core install into a new `full` extra. Lite mode (SQLite +
  in-process runner + local sandbox) needs neither: `bucker/temporal_compat.py`
  stubs the Temporal decorators when the SDK is absent (decorators become
  identity, real calls raise a clear error), and the asyncpg imports are
  lazy / `TYPE_CHECKING`-only. `pip install bucker-agent` stays minimal;
  `pip install bucker-agent[full]` (or `uv sync --extra full`) is required
  for the Temporal + Postgres stack. CI proves it: a new `lite-smoke` job
  installs base deps ONLY (guard asserts no temporalio/asyncpg present),
  boots `bucker lite`, runs a demo task end to end, and checks the event
  stream.
- **Launcher CI** — a new `launcher-windows` job runs the lite smoke
  through the real `start.bat` on `windows-latest`, exercising the exact
  double-click sequence (Python gate → venv → pip install → `bucker lite`).
  `scripts/ci_lite_smoke.py` is the shared harness (boot → demo task →
  verdict → event stream → kill).
- **Lite-mode security warnings** — the dashboard renders a red
  "LITE MODE — LOCAL EXECUTION" strip on every page when
  `BUCKER_SANDBOX_MODE=local`, and `bucker lite` prints the warning at
  startup. The README now carries a ⚠️ callout in the quickstart and a
  top-level **Known limitations** section (lite sandbox is not a security
  boundary, schedules need the full stack, experimental pieces, M2
  numbers unpublished, replay needs a prior live run).
- **`docker compose up --build` is truly one command** — a `sandbox`
  build-carrier service composes `bucker-sandbox:latest` (previously a
  hidden manual `docker build` prerequisite); the container exits
  immediately and the worker keeps launching sandboxes via the host
  socket.
- **`start.sh` gives the right Python install hint per platform** —
  detects brew/apt/dnf/pacman and prints the exact one-liner (Windows
  keeps `start.bat`, which auto-installs).

### Changed

- **Port consistency** — README and DEPLOYMENT docs now use `8123`
  everywhere (the launcher/dashboard default); the API/uvicorn examples
  no longer contradict the quickstart with `8000`.
- **`README-SIMPLE.md` rewritten lite-first** — install is "one thing:
  Python 3.11–3.13" and the quick start is `start.bat` / `./start.sh`;
  Docker + uv live in a clearly-labelled "full stack (advanced)" section.
  The old Docker-and-uv "Step 1" that contradicted the launchers is gone.
- Full-stack doc commands (`docs/DEPLOYMENT.md`, `docs/OPERATIONS.md`,
  `docs/WSL2_SETUP.md`, README full-stack sections) now include
  `--extra full` so the Temporal/Postgres clients are actually installed.
- `/usage` token-by-model table wrapped in a scroll container
  (`overflow-x: auto`, min-width on narrow viewports) so phones scroll
  instead of squishing.

### Fixed

- **DeepSeek live runs silently fell back to the fallback chain** — the
  catalog id `deepseek-v4-flash` was sent verbatim to api.deepseek.com,
  which returns empty content / drops the connection on real (large)
  worker prompts. New `_UPSTREAM_ALIASES` maps it to the real wire id
  `deepseek-chat` (and `deepseek-v3`), keeping the append-only catalog id
  stable in .env chains and replay digests (`bucker/gateway/registry.py`).
- **`patch -p0` fallback polluted the workspace with `a/`/`b/` dirs** —
  when git apply and patch -p1 both fail, patch -p0 keeps the diff's
  `a/`/`b/` prefixes and half-applies broken duplicates into literal
  `b/` dirs; the verifier's pytest then recursed into them and crashed
  collection (exit 2) even when the real files were correct, escalating
  good builds to needs_human_review. Both sandbox `apply_diff`
  implementations now best-effort-remove stray prefix dirs
  (`bucker/sandbox/runtime.py`, `bucker/sandbox/local.py`).
- **Graph tasks never left `pending`** (the headline reviewer bug) — the
  state fold's `_graph_step_completed` was purely informational, and the
  graph runner never emitted a terminal event, so a graph task folded to
  `pending` forever no matter what its steps did (children could be long
  `failed` while the parent never moved; the dashboard polled forever).
  The `__graph__` bookend now folds into task status (`started` →
  `in_progress`; `completed`/`failed` → terminal, with a graph that had
  failing steps reported as `failed`), and `record_graph_step` keeps the
  denormalized `tasks.status` row honest at the bookend, so `/tasks` list
  and `/tasks/{id}` agree. Regression tests in `tests/test_state.py`.
- **Lite SQL translator bound out-of-order placeholders wrong** (found
  while fixing the graph row update) — `translate_sql` replaced `$N`
  with `?` in appearance order, so a query like
  `SET status = $2 WHERE id = $1` silently bound the wrong args and
  updated zero rows. New `reorder_params()` in `bucker/lite/pool.py`
  reorders args to parameter order; unit + end-to-end tests in
  `tests/test_lite.py`.
- **Home page 500 in lite mode once any task existed** — `_index_stats`
  called `.isoformat()` on the per-day date, but the sqlite pool returns
  TEXT while asyncpg returns datetime. Accept both.
- **`/tasks/new` template dropdown could not produce a research task** —
  the Task-type `<select>` hardcoded `code_change`/`demo`, so the
  "Research with citations" card silently submitted as `code_change`
  (DOM no-op). Options are now generated from the same template registry
  the cards use, so they cannot drift again.
- **`verifier` was decorative** — the API accepted and persisted any
  string while the form implied a closed set. Now validated against the
  verifier registry (422 + the list), and the registry is populated at
  API startup (`register_builtins()`), which it previously was not.
- **CLI full-stack commands crashed raw in lite mode** — `bucker start`
  and `bucker graph run` leaked asyncpg/temporalio tracebacks. They now
  detect lite mode (and unreachable Postgres) and print the same
  actionable hint the HTTP API gives, pointing at
  `curl localhost:8123/...` or `uv sync --extra full && bucker dev`.
- **Schedules page blamed a connectivity problem for a by-design lite
  limitation** — the HTML page said "Temporal is not reachable — run
  `temporal server start-dev`" (which cannot fix lite mode); it now says
  schedules are a full-stack feature, matching the JSON API's message.
- **Auth banner overpromised** — "auth is BYPASSED for localhost" is not
  true of the OpenAI-compatible gateway (`/v1/*`), which always requires
  the token; the banner now says so.
- **`Structured extraction` template copy promised a schema verifier
  that does not exist** (registered verifiers: `noop`,
  `python_test_runner`, `citation_checker`) — the description now states
  what actually happens (tests the worker writes are verified).

- **`scripts/doctor.py` was Windows-only** — every venv check hardcoded
  `.venv/Scripts/python.exe`, so on Linux/macOS/WSL2 doctor reported
  ".venv is missing" even after a correct `uv sync` and skipped every
  downstream check. The venv layout and pyvenv.cfg base-interpreter name
  are now platform-aware (`os.name`); regression tests in
  `tests/test_doctor.py` cover both layouts.

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
