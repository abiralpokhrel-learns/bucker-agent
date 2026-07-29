# Project Summary — Durable, Verified Agent Platform

*One page. Everything decided in this session, in order. (Baikuntha · July 27, 2026)*

## What this is

An open-source platform for running AI agents where **nothing is trusted until it's verified, nothing is lost when it crashes, nothing overspends silently, and nothing changes in production until it's proven better.** Every task is an append-only event stream (the source of truth); a Planner turns fuzzy goals into typed, schema-validated Task contracts; a Worker executes in a sandbox; a domain-specific Verifier gates every result (code → tests/lint; repeated failure → human review); every model/tool output is stored verbatim so any run can be replayed deterministically; and improvements only ship through benchmark → human approval → promotion, with rollback retained. The LLM is a replaceable plugin; the durability + verification + evidence layer is the product.

## The verdict

**Build it — as a 12-week timeboxed bet, not a leap of faith.** The gap is real (LangGraph/CrewAI checkpoints still criticized as not-durable in 2026), but incumbents are moving (Temporal's AI push, Dapr Agents/NVIDIA) and observability tools are crowded. **Do not build "a better Hermes/OpenClaw"** — solo vs. a $1.5B-backed lab and the year's most viral harness is unwinnable on features. Instead: be the layer underneath them. Hermes/OpenClaw-style loops become your baselines and pluggable workers — "your agent, but it survives crashes, can't merge unverified work, and comes with receipts." Nobody in the crowd ships verification-gated execution + a reproducible architecture-comparison benchmark. That's the wedge.

## The plan at a glance

| Stage | Weeks | Builds | Gate |
|---|---|---|---|
| A — Setup | 0 | Temporal + Postgres + repo, learn the engine | — |
| B — Durable core | 1–3 | Event log, snapshots, idempotent workflow, crash test in CI | **M1:** `kill -9` → resume, zero loss |
| C/D — Plan→Work→Verify | 4–8 | Typed contracts, model router, sandbox, planner, worker, verifier, **replay engine** | Replay = identical outcome |
| E — Benchmark | 9–12 | Baseline agent, SWE-bench Lite paired harness, honest stats | **M2 (go/no-go):** beat single-agent baseline on same model — or stop, revise once, re-gate. Publish either way |
| F — Scheduling & observability | 13–17 | Budget/deadline hard-stops, telemetry dashboard, adaptive retry | **M3:** repeat-failure rate measurably drops |
| G — Second domain & promotion | 18–26 | Second verifier, candidates pipeline, promote/rollback, replay regression gate | **M4:** promote→regress→rollback proven; `v1.0` |

Full detail: `solo_build_guide.md` (phase guide) and `BUILD_PLAN.md` (40 checkbox micro-steps with definitions of done).

## Working rules

Steps are tagged `[HAND]` (write it yourself: event store, idempotency, replay, verifiers, stats) or `[VIBE]` (generate freely: configs, CI, dashboards, plumbing). The meta-rule is your own platform's principle applied to you: **vibe-coded output is untrusted worker output — the test suite is its verifier.** Never skip a definition of done. Friday ritual: crash test + replay spot-check green, update `decisions.md`, push a public changelog. Build in public from day 1 — that *is* the go-to-market.

## Economics & downside

~16–19 working weeks of build inside a 26-week window. Costs: LLM API only — tens of dollars per iteration benchmark run (cheap model, 25–50 instances), low hundreds for the one frontier-model headline run; hardware 16 GB RAM / 100+ GB disk. **Bounded worst case:** ~3 months + a few hundred dollars → deep Temporal/event-sourcing/eval skills, a public benchmark harness, and a rigorous write-up. A negative result published well is still a win; only silence is a loss.

## Kill criteria (pre-committed)

M1 undemonstrable by week 3 → re-estimate everything. No end-to-end benchmark by week 12 → descope to "durable verified task runner," re-gate in 3 weeks. M2 negative after one revision → publish and pivot to the benchmark harness as the product. Two zero-commit weeks → shrink the weekly goal, not the vision.

## Next action

Step 1 of `BUILD_PLAN.md`: install the Temporal CLI, run `temporal server start-dev`, open `localhost:8233`. Tonight.
