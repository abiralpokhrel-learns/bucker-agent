# Changelog

All notable changes to bucker-agent are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
