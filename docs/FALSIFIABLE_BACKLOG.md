# Falsifiable backlog — done means provable, not directional

Source: the six items with concrete stopping conditions, plus the pulled-forward
interrupted-stream recovery spec (`docs/INTERRUPTED_STREAM_RECOVERY.md`, cases R1–R10).
Each item lists its Done check and its dependencies. Do not reorder past dependencies
expecting clean data.

## 0. Interrupted-stream recovery (pulled forward — build first)
Done: R3 + R4 pass with evidence — stream breaks after partial tool arguments
(and valid-JSON-but-incomplete stream) → zero dispatches for interrupted proposals,
zero proposal-caused writes, no successful finish, no silent fallback; error event
carries `interrupted=True, discard_partial=True` with the honest message
("This attempt was interrupted. Pending tool proposals were not run…"), never a
"retrying" claim when no retry started.
Depends on: nothing. Blocks: #1 (task loop), #4 (routing policy).

## 1. One real task, end to end
Done: prompt → provider call → proposed edit → approve → test runs → pass/fail shown
→ close app → reopen → session state intact. Plus three fault runs: force fallback
mid-task, kill app mid-task, interrupt stream after partial tool arguments (see #0
for defined behavior). Instrumentation (TTFT, total latency, cache-hit/miss where
reported) built in now — adding after means re-running everything.
Depends on: #0. Blocks: #4 (needs its data), #5 (don't package a flow you'll change).

## 2. Validated provider catalog
Done: every entry has verified endpoint, current pricing, confirmed tool-calling
support, confirmed streaming behavior — not "listed somewhere online". Domain
allowlist baked in from start: manifest updates metadata, never redirects a stored
key's base URL to an unlisted host (`follow_redirects=False`, allowlisted base URLs,
OpenRouter `:free` suffix enforced).
Depends on: nothing (partial needed before #4). Blocks: #3 (onboarding tests against it).

## 3. Onboarding with connection tests + recovery instructions
Done: bad key produces specific actionable message (expired, wrong scope,
rate-limited, wrong region), not generic failure. Built against catalog from #2 so
the test says what's actually wrong.
Depends on: #2. Blocks: nothing.

## 4. Measure before changing routing policy
Done: real answer from #1's instrumentation run across >1 provider (needs #2
partially done first). One run is not a policy decision.
Depends on: #1 (data), #2 (partial). Blocks: any routing-policy change.

## 5. Installer + clean-machine testing
Done: machine with nothing pre-installed, timed from download to first successful
agent-driven edit.
Depends on: #1 solid. Blocks: nothing.

## 6. Verified mode with labeled local-vs-Docker boundary
Done: label accurate at every point in flow, not just toggle — a step silently
running local when user believes sandboxed fails this item (label worse than none).
Depends on: #1 (task flow). Blocks: nothing.

## Free-model latency — workarounds in this slice (no quality change)
Free tiers are slow because of queue tail (TTFT 10-30s silent), not token speed.
What landed:
- TTFT budget (`BUCKER_GATEWAY_TTFT_S=20`): silent candidate abandoned before first
  token → safe fallback; post-TTFT deadline governs, never switches mid-flight.
- Keepalive pools (20 conn / 10 keepalive / 30s) + OpenRouter identify headers:
  saves 200-500ms TLS per sequential call (3-5 calls/task).
- `stream_options.include_usage` always on streams + `cached_tokens` preserved:
  cost/latency analysis no longer records 0/NULL for streamed calls.
- TTFT + total latency + cache-hit/miss in `gateway_usage` (mig 006), `telemetry`
  (mig 007), `InferenceResponse`, `ModelResponse`, recordings.
- Critique fast-path (`BUCKER_CRITIQUE_MODEL`): 600-token gate runs on fast model
  instead of slow free primary; saves one full 20-50s sequential call.
What NOT to do here: hedged parallel requests (doubles free-quota burn), prompt
truncation changes (invalidates recordings), routing-policy switch (needs #4 data).
Measure with: per-attempt TTFT vs total in ledger; compare `latency`/`balanced`
only after #1 data across 2+ providers.
