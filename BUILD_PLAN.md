# BUILD_PLAN.md — 40 micro-steps to v1.0

Drop this file in your repo root. Tick boxes as you go. One step at a time, in order — every step has a **DoD** (definition of done) and you never move on with an unmet DoD. Steps are sized ~1–3 days each at near-full-time pace.

**Tags — how to split vibe-coding vs hand-writing:**

- `[HAND]` — write it yourself, understand every line. These are the parts where a subtle bug silently poisons everything downstream (event log, determinism, idempotency, replay, verification, stats).
- `[VIBE]` — safe to generate: boilerplate, configs, plumbing, dashboards. Review the diff, run the tests, move on.
- `[MIX]` — vibe the skeleton, hand-write the marked core.

**The meta-rule (your own platform's principle, applied to you):** vibe-coded output is untrusted worker output. Your test suite — the crash test, the property tests, the replay test — is *your* verifier. Nothing merges without passing it, no matter who wrote it, you or the model. If you can't explain a line in `core/`, `workflows/`, or `replay/`, rewrite it until you can.

Milestones: **M1** = step 12 · **M2 (go/no-go)** = step 30 · **M3** = step 34 · **M4** = step 39.

---

## Stage A — Setup (Week 0) · steps 1–4

- [ ] **1. Environment bootstrap** `[VIBE]` (0.5d)
  Install Temporal CLI, Docker, uv. Run `temporal server start-dev`.
  **DoD:** Temporal UI loads at `localhost:8233`; `docker run hello-world` works.

- [ ] **2. Repo scaffold** `[VIBE]` (0.5d)
  uv project (Python 3.12), deps (`temporalio asyncpg pydantic jsonschema litellm fastapi uvicorn pytest`), folder tree from the guide, Apache-2.0, README with the one-liner, public GitHub repo, `docs/decisions.md`.
  **DoD:** `uv run pytest` runs (0 tests, green); repo is public.

- [ ] **3. Postgres up** `[VIBE]` (0.5d)
  `docker-compose.yml` with Postgres 16 + volume; create `app_rw` role.
  **DoD:** `psql` connects as `app_rw` from host.

- [ ] **4. Learn the engine** `[HAND]` (2d)
  Temporal's free 102 course (Python). Then write a throwaway hello-workflow with one activity, run it, kill the worker, watch it resume.
  **DoD:** you can state the workflow-determinism rule and the workflow-vs-activity split from memory in `decisions.md`.

## Stage B — Phase 0: durable core (Weeks 1–3) · steps 5–12

- [ ] **5. Event schema migration** `[MIX]` (1d)
  Vibe the migration file from the guide's SQL (events / tasks / snapshots, indexes); hand-review every column against Database Design doc 13. Grant `app_rw` INSERT+SELECT only on `events`.
  **DoD:** migration applies clean twice (idempotent); `UPDATE events ...` as `app_rw` **fails**.

- [ ] **6. Event store: append + read** `[HAND]` (1d)
  `core/eventstore.py`: `append()`, `read_stream(task_id)` ordered by `id`.
  **DoD:** unit tests: ordering, payload round-trip, schema_version stamped.

- [ ] **7. State reconstruction** `[HAND]` (1–2d)
  `rebuild_state(events) -> dict`: a pure fold, one handler per event type.
  **DoD:** table-driven test: event list → exact expected state; unknown event type raises (never silently skipped).

- [ ] **8. Snapshots** `[HAND]` (1d)
  Snapshot every 50 events; rebuild = latest snapshot + tail.
  **DoD:** property test: snapshot-path state == full-replay state for random event streams.

- [ ] **9. Blob store** `[VIBE]` (0.5d)
  Content-addressed files in `blob/` (sha256 name), `put() -> ref`, `get(ref)`. This later becomes S3 without touching the event schema.
  **DoD:** round-trip test; same content → same ref (dedup).

- [ ] **10. First durable workflow** `[HAND]` (2d)
  `workflows/task_workflow.py`: 5 fake steps, each an activity that does a fake side effect + appends an event. All I/O in activities; workflow code deterministic.
  **DoD:** one run produces a complete, ordered event stream visible in both Postgres and the Temporal UI.

- [ ] **11. Idempotent activities** `[HAND]` (1d)
  Idempotency key = `(task_id, step_n)`; re-executed activity must not double-append or redo the side effect.
  **DoD:** test that forces an activity retry → exactly one event, one side effect.

- [ ] **12. Crash test + CI = M1** `[MIX]` (2d)
  `tests/crash_test.sh`: start task → `kill -9` worker between steps 3–4 → restart → task completes. Vibe the GitHub Actions yaml (Postgres + Temporal dev server as services); hand-write the assertions (no gaps, no dups, state correct). Record perf: event write <50ms p95, rebuild <500ms @1k events, resume <5s.
  **DoD:** crash test green in CI; perf table in README. Tag `v0.1.0`. Publish build-log post #1.

## Stage C — Phase 1a: contracts & AI plumbing (Weeks 4–6) · steps 13–19

- [ ] **13. Typed Task contract** `[MIX]` (1d)
  `contracts/task.schema.json` (doc 11 shape: task_type, objective, files, constraints, budget, deadline_minutes, verifier) + `schema_version`; vibe the pydantic mirror models.
  **DoD:** fixture suite of valid/invalid Tasks passes/fails as expected.

- [ ] **14. Model router** `[VIBE]` (1d)
  Thin LiteLLM wrapper: model name from config/env ONLY; every raw request+response stored verbatim to blob, `ModelCallCompleted`/`ModelCallFailed` events with cost+latency telemetry.
  **DoD:** switching models = editing one env var; grep proves no model name in code.

- [ ] **15. Recorded-mode client** `[HAND]` (1d)
  Dev mode where the router answers from stored blobs instead of live API — the seed of the replay engine, and it makes iteration free.
  **DoD:** full pytest suite runs with network disabled.

- [ ] **16. Planner** `[HAND]` (2–3d)
  Activity: objective + current state → typed Task. Validate against schema; on failure log `SchemaValidationFailed` + one re-prompt; then fail the task. Prompt lives in a versioned file, not inline.
  **DoD:** ≥8/10 sample objectives yield schema-valid Tasks; every failure visible as an event.

- [ ] **17. Sandboxed tool runtime** `[MIX]` (3d)
  Per-task Docker container: no host mounts beyond the task workspace, no network by default. Exec API: run command, read/write file, apply diff. All stdout/stderr/exit codes verbatim to blob (`ToolCallCompleted` events). Hand-write the isolation config; vibe the plumbing.
  **DoD:** smoke escape test — container cannot read a host canary file; outputs retrievable by ref.

- [ ] **18. Secret scanning** `[VIBE]` (0.5d)
  Regex pass (key patterns) over tool/model outputs before blob write; flag + redact.
  **DoD:** planted fake AWS key never reaches disk unredacted.

- [ ] **19. Worker** `[HAND]` (2d)
  Activity: one typed Task → structured result (unified diff + status), schema-validated before `WorkerCompleted` commits it to state.
  **DoD:** end-to-end on a toy repo: objective → plan → worker diff recorded in event log.

## Stage D — Phase 1b: the verification loop (Weeks 7–8) · steps 20–24

- [ ] **20. Verifier plugin interface** `[HAND]` (1d)
  `verifiers/base.py`: `verify(task, result, sandbox) -> {passed, diagnostics}`; registry maps verifier name → impl; workflow routes `VerificationRequested` by task's registered verifier (FR7).
  **DoD:** dummy always-pass/always-fail verifiers registered and routed by name in tests.

- [ ] **21. python_test_runner** `[HAND]` (2d)
  Apply diff in sandbox → run pytest → pass/fail + failing test names as structured diagnostics (`VerificationPassed`/`VerificationFailed`).
  **DoD:** known-good diff passes; known-broken diff fails with correct test names captured.

- [ ] **22. Retry → human escalation** `[HAND]` (1d)
  `VerificationFailed` → `RetryScheduled` (failure context fed back to planner) → max 2 retries → `NeedsHumanReview`. Fixed policy only; adaptive is step 34.
  **DoD:** task rigged to always fail ends in `NeedsHumanReview` with the full retry audit trail in events.

- [ ] **23. Replay engine** `[HAND — crown jewel]` (2–3d)
  `replay/engine.py`: re-run any completed/failed task answering every model/tool call from stored outputs (never live). Report match/mismatch vs original verification outcome (FR5).
  **DoD:** live run → replay → identical outcome; tampering with one stored blob → mismatch correctly reported.

- [ ] **24. Minimal API** `[VIBE]` (1d)
  FastAPI: `POST /tasks`, `GET /tasks/{id}` (status + cost_so_far), `GET /tasks/{id}/events`, `POST /tasks/{id}/replay`. Bearer token.
  **DoD:** happy path via curl; this becomes the README quickstart.

## Stage E — Phase 1c: benchmark & the gate (Weeks 9–12) · steps 25–30

- [ ] **25. Baseline single-agent loop** `[HAND]` (2d)
  Deliberately simple mini-SWE-agent-style loop: same model, same sandbox, same tools, no planner/verifier structure. Keep it small and honest — a weak strawman baseline invalidates your whole claim.
  **DoD:** solves ≥1 SWE-bench Lite instance end-to-end.

- [ ] **26. SWE-bench integration** `[MIX]` (3d)
  Instance loader, repo checkout into sandbox, patch extraction from both systems, evaluation via the **official harness** (`swebench.harness.run_evaluation`, Docker, `--max_workers 8`, watch disk; `--cache_level=base` if tight).
  **DoD:** one instance runs through platform AND baseline, both graded by the official harness.

- [ ] **27. Paired benchmark runner** `[HAND]` (2d)
  Same N instances through both systems; experiment log per doc 16 (run_id, architecture, model, task set, success, cost, latency, recovery).
  **DoD:** 5-instance paired smoke run completes unattended, results in the log.

- [ ] **28. Stats module** `[HAND — never vibe your stats]` (1–2d)
  Paired outcomes table, McNemar's test + bootstrap CI on success delta; cost/success from your own telemetry.
  **DoD:** unit tests against hand-computed fixtures give exact expected p-values/CIs.

- [ ] **29. Iteration loop** `[MIX]` (5–8d)
  25–50 instances, cheap model. Diagnose failures from event streams (that's the product working for you), fix planner prompts / worker tools / chunking, re-run. Keep every run in the experiment log; build a failure taxonomy in `decisions.md`.
  **DoD:** ≥3 logged iterations with improving or understood numbers.

- [ ] **30. THE GATE = M2** `[HAND]` (3d)
  Full paired run with a frontier model. Apply your decision rule verbatim: proceed only on statistically meaningful success-rate improvement or clearly favorable cost/success tradeoff; otherwise revise architecture once and re-gate. **Publish either way** — methodology, harness, numbers, reproduce-it-yourself instructions. Launch: quickstart README, post to HN/r/MachineLearning/Temporal community.
  **DoD:** decision recorded in `decisions.md`; benchmark post live; `v0.5.0`.

## Stage F — Phase 2: scheduling & observability (Weeks 13–17) · steps 31–34

- [ ] **31. Telemetry formalized** `[VIBE]` (1d)
  `telemetry` table per doc 13 (event_id, model_used, tool_used, latency_ms, cost_usd, verification_result); backfill capture points from steps 14/17/21.
  **DoD:** every model/tool event has a telemetry row; per-task cost query is one SQL statement.

- [ ] **32. Budget/deadline enforcement** `[HAND]` (2–3d)
  Scheduler checks cumulative cost/time; on breach halt + `BudgetExceeded`/`DeadlineExceeded` (UC-6). Test under 10 concurrent tasks, not just isolation.
  **DoD:** task with $0.10 budget provably halts at $0.10 under concurrent load.

- [ ] **33. Telemetry dashboard** `[VIBE]` (2–3d)
  One FastAPI HTML page per task: what happened, why, how long, how much, replay link (doc 08's single-view requirement). Aggregate: success rate + cost by task type over time. No React.
  **DoD:** you debug a real failed task using only the dashboard.

- [ ] **34. Adaptive planning = M3** `[HAND]` (4–5d)
  On repeated failure vary strategy: switch model / chunk smaller / ask clarification. A/B vs fixed retry on an injected-failure task set (flaky tool, ambiguous objectives).
  **DoD:** measured reduction in repeat-failure rate, logged as an experiment; `v0.6.0`.

## Stage G — Phase 3: second domain & promotion pipeline (Weeks 18–26) · steps 35–40

- [ ] **35. Second verifier** `[HAND]` (4–5d)
  Citation-consistency (or docs-consistency) verifier — pick by what early users ask for. Subjective ground truth: validate the verifier itself against a small human-labeled fixture set first (doc 15), and keep `NeedsHumanReview` in the loop.
  **DoD:** verifier agrees with your labels on ≥90% of fixtures; disagreements documented.

- [ ] **36. Evaluation scorer + candidates table** `[HAND]` (2d)
  Score recent outcomes from the event log; `candidates` table (proposed/benchmarked/approved/rejected/promoted/rolled_back).
  **DoD:** a real weakness surfaced from production events becomes a candidate row.

- [ ] **37. Candidate benchmarking + approval** `[VIBE — reuses 27]` (2d)
  Run candidate config vs current production config through the paired harness; approval = recorded human sign-off (you) via CLI.
  **DoD:** candidate row carries benchmark_result + approval record.

- [ ] **38. Promotion + rollback** `[HAND]` (2d)
  Atomic config flip promoted → active; `rolled_back` restores prior config; both are events.
  **DoD:** promote → rollback round-trip leaves system state identical to pre-promotion.

- [ ] **39. Replay regression gate = M4** `[HAND]` (2–3d)
  Before any promotion: replay a fixed suite of past workflows under the candidate; any previously-passing task failing → promotion blocked + alert (docs 20/22).
  **DoD:** a deliberately regressive candidate is auto-blocked in CI; end-to-end promote→regress→rollback demonstrated.

- [ ] **40. v1.0** `[MIX]` (3d)
  Write-up of the whole arc (M1→M4 with numbers), CONTRIBUTING.md, good-first-issues, roadmap issue for community, tag `v1.0.0`.
  **DoD:** a stranger can go from `git clone` to a verified task + replay in under 10 minutes.

---

## Running totals & sanity check

Estimates sum to ~75–90 working days ≈ 16–19 weeks at your pace, inside the 26-week window with slack for the unplanned 30% (there is always an unplanned 30%). If a step blows past 2× its estimate, stop and split it — that's the plan failing, not you.

**Weekly ritual (every Friday):** crash test + replay spot-check green → append to `decisions.md` → push a short public changelog. **Never skip a DoD to feel fast.**
