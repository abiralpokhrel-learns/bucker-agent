# Changelog

All notable changes to bucker-agent are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
