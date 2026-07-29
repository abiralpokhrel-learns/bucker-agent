# Decision log

Append-only, like the event log it describes. One entry per decision that would
be expensive to reverse or confusing to rediscover. Date, decision, why, and
what would make you change your mind.

Add an entry every Friday even if it is one line. Six months from now this file
is the only thing that will remember *why*.

---

## 2026-07-27 — Project kickoff, name, and scope

**Decision:** Build the durable-execution + verification platform from the 24-doc
package, solo, near-full-time, in Python. Working name **bucker-agent**.

**Why:** The gap identified in the market research is still open (checkpoint-based
frameworks are still being criticised as not-durable in 2026), but the harness
layer is crowded and moving fast.

**Changes my mind:** M2 (the benchmark gate) coming back negative twice.

---

## 2026-07-27 — Position underneath Hermes/OpenClaw, not against them

**Decision:** Do not compete on agent-harness features. Hermes-style and
OpenClaw-style loops become *baselines and pluggable workers*. bucker sells the
layer beneath: durability, verification gating, cost bounding, benchmarking.

**Why:** Nous Research is raising at ~$1.5B and OpenClaw has enormous community
momentum. A solo builder loses a feature race. Neither is crash-durable,
verification-gated, or reproducibly benchmarked — and the observability market
has tracing tools, not architecture-comparison harnesses.

**Changes my mind:** Either project shipping durable execution + domain
verification as a first-class, well-tested feature.

---

## 2026-07-27 — Temporal for durable execution, Postgres for the event log

**Decision:** Two layers, deliberately separate. Temporal makes *workflow
execution* durable (retries, timeouts, resume). The Postgres event log is the
*product's* source of truth — the thing users query, replay, and audit.

**Why:** Temporal is rented plumbing. The event log, contracts, verifiers, and
benchmark harness are the durable value. Conflating them would make the product
a Temporal wrapper, which is not defensible.

**Changes my mind:** Nothing likely in Phase 0–2. If Temporal Cloud costs bite
later, the engine is swappable precisely because of this separation.

---

## 2026-07-27 — `idempotency_key` added to the events table

**Decision:** Deviate from Database Design doc 13 by adding
`events.idempotency_key` plus a partial unique index on
`(task_id, idempotency_key)`.

**Why:** BUILD_PLAN step 11 requires activity retries to be safe. Without a
uniqueness constraint at the database level, a worker crashing between "side
effect performed" and "event appended" produces either a lost event or a
duplicate on retry. The unique index makes `append` exactly-once, which is what
lets the crash test assert "every step recorded exactly once."

**Changes my mind:** Nothing. This is strictly stronger than the original design.

---

## 2026-07-27 — Unknown event types raise instead of being skipped

**Decision:** `rebuild_state` raises `UnknownEventType` for any event with no
fold handler. A test asserts every `EventType` member has a handler.

**Why:** Skipping unknown events is the tempting default (it keeps old streams
working after a refactor), but it means reconstructed state silently diverges
from what actually happened — the precise failure this architecture exists to
prevent. Loud beats convenient.

**Changes my mind:** Cross-version replay of very old streams becoming a real
operational need; then the answer is version branching in the handler, still
never a silent skip.

---

## 2026-07-27 — Append-only enforced by database grants, not convention

**Decision:** The application connects as `bucker_app`, which holds `INSERT` and
`SELECT` on `events` and nothing else. `UPDATE`/`DELETE`/`TRUNCATE` are revoked.
A test asserts those statements raise `InsufficientPrivilegeError`.

**Why:** "The event log is immutable" is a claim the whole architecture rests
on. A claim enforced only by developer discipline is a claim that breaks the
first time someone is in a hurry at 2am.

**Changes my mind:** Nothing.

---

## 2026-07-27 — `model_mode=recorded` is the default

**Decision:** The model router defaults to replaying stored responses rather
than calling a provider. `live` is opt-in.

**Why:** Solo budget. Most iteration should cost zero, and tests must never hit
the network. It also means the replay engine is exercised constantly from day
one instead of being a feature bolted on in week 8.

**Changes my mind:** Nothing.

---

## 2026-07-29 — Activity timeouts are the crash-recovery dial

**Decision:** Demo activity `start_to_close_timeout` cut from 2 minutes to 15
seconds. `RESUME_TIMEOUT` in the crash test raised to 180s.

**Why:** The first real M1 run stalled. When a worker dies mid-activity,
Temporal cannot know the process is gone — it waits for `start_to_close_timeout`
to expire before rescheduling the activity elsewhere. With a 2-minute timeout
and a 120-second wait in the crash test, resume could never finish inside the
window. The architecture was fine; the timeout was mis-tuned and the test's
patience was shorter than the recovery it was measuring.

**Rule going forward:** for short activities, keep `start_to_close_timeout`
tight. For genuinely long ones (a test suite, a model call), do NOT simply
raise it — heartbeat from inside the activity and set `heartbeat_timeout`, so
worker death is detected in seconds no matter how long the work legitimately
takes. This matters more in Phase 1, where a verifier running pytest may take
minutes.

**Changes my mind:** Nothing. This is a straightforward tuning bug, and the
crash test earned its keep by catching it on day one.

**Postscript:** the timeout was real but it was not the whole story — see the
next entry. Shortening it alone did not make M1 pass.

---

## 2026-07-29 — Crash injection must be one-shot

**Decision:** `should_inject_crash()` writes a `<step>.crashed` marker to disk
and returns True at most once per (workspace, step). Extracted from `run_step`
so it is unit-testable; covered by `tests/test_crash_injection.py`.

**Why:** The second M1 run still failed, with the event stream frozen at 7
events for the full 180 seconds — `TaskCreated`, `TaskStarted`, then
`fetch` started/completed, `analyze` started/completed, `transform` started,
and nothing further. A stalled-but-healthy system would have produced *some*
progress after the restart. Zero progress meant no worker was alive.

The cause: `crash_at` is part of the workflow input, and Temporal replays a
retried activity with the identical input. So the restarted worker reached
`transform`, matched `crash_at` again, and killed itself — as would every
worker after it. The injection was designed as "crash at this step" when it
needed to be "crash at this step, once."

**Wider lesson:** anything a retried activity reads from its input is, by
definition, replayed. State that must not repeat has to live somewhere that
survives process death — on disk, or in the event log. This will matter again
in Phase 1 for any worker action that must not be re-attempted.

**Test-design lesson:** the crash test now watches the restarted worker's
liveness during the wait instead of only blocking on the result. A dead
replacement worker is a specific diagnosis; "timed out" is not. Diagnostics
that distinguish failure modes are worth writing before you need them.

**Changes my mind:** Nothing.

---

## 2026-07-29 — Repository layout and vendored binaries

**Decision:** One git repository, rooted at the project folder. `temporal.exe`
is never committed — the CLI is installed on PATH.

**Why:** The first setup produced nested repositories (one at the projects
root, one at the project) and a 141 MB `temporal.exe` inside the tree, which
git compressed to a ~47 MB object. GitHub rejects files over 100 MB outright,
and a large blob in history is permanent — every future clone pays for it. The
outer repository also tracked the project as a bare gitlink, so the pushed
repository contained no actual source.

**Changes my mind:** Nothing.

---

## 2026-07-29 — M1 passed; recorded resume time is 16s, not the 5s target

**Observed:** M1 green. Crash at `transform` at +0.49s, next step completed at
+16.61s, all five steps present, exactly one crash marker, no duplicates.

**The honest number:** the source docs target "resume in under 5 seconds"; the
measured wall-clock gap is ~16s. State reconstruction itself was effectively
instant — the 16 seconds is almost entirely Temporal *detecting* the dead
worker, which it cannot do before `start_to_close_timeout` (15s) expires.

**Decision:** record 16s rather than quietly retuning the timeout to hit the
target. Chasing the number by shrinking the timeout would make long activities
flap. The real fix is heartbeating (Phase 2, step 32-ish), which detects worker
death in seconds independent of how long the work legitimately takes.

**Bonus observation:** the blob store deduplicated across runs — `fetch` and
`analyze` outputs from the failed run were byte-identical to the successful
run's, so no new blobs were written. Content addressing working as designed,
unprompted.

---

## 2026-07-29 — Prompts use string.Template, not str.format

**Decision:** Prompt templates use `$name` placeholders rendered with
`string.Template.safe_substitute`.

**Why:** The planner prompt is mostly a JSON schema. `str.format` treats every
`{` in that schema as a placeholder and raises `KeyError` on the first brace.
The planner test suite caught this on its very first run, before any model was
ever called — which is the argument for writing the fake-router tests before
wiring up a live provider.

`safe_substitute` over `substitute` so an unrecognised `$token` in a prompt is
passed through rather than killing a live task at runtime.

**Changes my mind:** Nothing.

---

## 2026-07-29 — Recorded mode never falls back to live

**Decision:** A missing recording raises `RecordingMissing`. It never silently
issues a live call.

**Why:** The fallback is superficially friendlier and quietly catastrophic: a
test suite advertised as free and deterministic would start costing money and
varying between runs, and nobody would notice until the bill or a flaky
benchmark arrived. Editing a prompt changes the request digest and therefore
requires a fresh recording — that is the intended behaviour, not a bug.

The router also verifies that a recorded response still hashes to its blob ref
before replaying it, so a corrupted or tampered archive surfaces as an error
instead of being replayed as truth.

**Changes my mind:** Nothing.

---

## 2026-07-29 — Always invoke tools as `python -m <tool>`

**Decision:** Docs, scripts, and habit use `uv run python -m pytest` /
`python -m ruff`, never the generated `pytest.exe` / `ruff.exe` shims.

**Why:** Two separate failures on Windows in one sitting, both from those shims.

First, moving the repo out of OneDrive broke every launcher: `uv` writes them as
trampolines with the venv's absolute path baked in, so they cannot canonicalize
their own script path once the folder moves. Rebuilding `.venv` fixed that one.

Then the freshly-built launchers were blocked outright by Windows Smart App
Control — `os error 4551` — because they are unsigned executables that appeared
from nowhere. Rebuilding cannot fix that; the policy objects to the shim
existing at all.

`python -m` routes through the signed interpreter, so neither failure applies,
and it behaves identically on Linux and macOS. No security setting was changed
to work around this: Smart App Control is a real boundary, and on Windows 11
disabling it is one-way (a reinstall to restore).

**Changes my mind:** Nothing. This is strictly more portable than the shims.

---

## <!-- next entry: date, decision, why, what changes my mind -->
