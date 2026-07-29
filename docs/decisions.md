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

## <!-- next entry: date, decision, why, what changes my mind -->
