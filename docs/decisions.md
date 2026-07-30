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

## 2026-07-29 — Sandbox security lives in testable arguments

**Decision:** All container isolation is expressed in `build_run_args()`, a pure
function, and asserted on directly by `tests/test_sandbox.py`.

**Why:** Security posture written as configuration inside a method body is
verified by nobody and quietly erodes — someone adds `--network bridge` to debug
something and it survives into main. As a pure function returning argv, every
property is a unit test that runs on every push, including on machines without
Docker. The defaults: no network, all capabilities dropped, no-new-privileges,
non-root user, read-only root filesystem, size-capped noexec `/tmp`, memory and
swap and CPU and PID limits, and exactly one mount — the task's own workspace.

**Standing rule:** deleting one of those tests requires a decisions.md entry
explaining why the risk it covered became acceptable.

**Changes my mind:** Nothing. When the harness needs network access for
dependency installs (SWE-bench, step 26), that becomes an explicit per-task
opt-in that is visible in the argv and therefore in the event log.

---

## 2026-07-29 — Worker status is "produced", never "success"

**Decision:** The worker result contract uses `produced | blocked |
no_change_needed`, and `blocked` requires a `blocked_reason`.

**Why:** The word choice is deliberate. "Success" invites downstream code — and
downstream readers — to treat the worker's own report as evidence. It is a
claim, and the verifier is what turns a claim into state. Naming matters most
in the places where the architecture exists specifically to resist a model's
confidence.

`blocked` being first-class is the other half: a worker that cannot do the task
must say so with a reason. An invented plausible diff burns a full verification
cycle and teaches the system nothing, so the schema makes honesty structurally
easier than fabrication.

**Changes my mind:** Nothing.

---

## 2026-07-29 — Secret scanning happens before the write, not after

**Decision:** `redact()` runs inside the sandbox's output capture, before any
tool output reaches blob storage or the event log.

**Why:** The event log is append-only and permanent. A credential written into
it cannot be deleted — only tombstoned by a compensating event, with the
original blob still on disk. There is no cleanup pass that undoes this, so the
scan must be upstream of the write.

Scope stated honestly: this catches known-shaped credentials (AWS, GitHub,
`sk-` keys, Slack, Google, JWTs, PEM blocks, labelled assignments, connection
strings). It will not catch a password that looks like an English word. It is
defence in depth, not a substitute for keeping real secrets out of the sandbox
via a secrets manager.

False positives are accepted deliberately — redacting something harmless costs
a line of log noise; missing a live key costs a rotation and an incident.
Placeholders (`CHANGEME`, `${VAR}`, `your_token_here`) are excluded, because a
scanner that flags documentation trains people to ignore it.

**Changes my mind:** Evidence that the false-positive rate is high enough to
obscure real findings in practice.

---

## 2026-07-29 — A verifier never asks a model, enforced by test

**Decision:** Nothing in `bucker/verifiers/` may import the router, litellm, or
the planner. A test walks the package's AST and fails the build if it does.

**Why:** The moment a verifier asks an LLM "does this look right?", the system
is a model grading itself, and every benchmark number published after that
point is meaningless — including the M2 comparison the whole project is staked
on. This is the single assumption most likely to be eroded by a well-meaning
convenience ("just ask the model, it's easier than parsing pytest output"), so
it is enforced structurally rather than remembered.

**Changes my mind:** Nothing for objective domains. Phase 3's second domain
(citation checking) may need a judgement call that is not purely mechanical —
if so, that verifier gets a *human-labelled fixture set* to validate against
first (step 35), and the LLM-in-the-loop question gets its own decision entry
and its own bias review. It does not get grandfathered in quietly.

---

## 2026-07-29 — An empty test suite is a failure, not a pass

**Decision:** `python_test_runner` fails verification when pytest collects no
tests, regardless of exit code.

**Why:** Green-because-nothing-ran is the most dangerous false pass available.
A worker that deletes the tests, or writes a diff that breaks collection, would
otherwise be rewarded for it — and the reward-gaming risk in the Risk
Assessment is exactly this shape. The verifier also ignores the worker's own
`summary` field entirely; a test asserts that a boastful summary alongside
failing tests still fails.

**Changes my mind:** Nothing.

---

## 2026-07-29 — Retry policy is pure, and ceilings outrank everything

**Decision:** `bucker/retry.py` is a pure function from `AttemptState` to a
`Decision`. The workflow executes decisions and holds no policy of its own.
Budget and deadline breaches are evaluated *before* success.

**Why:** Policy smeared through orchestration code cannot be unit-tested, and
this policy has precedence rules that are easy to get subtly wrong. Two of the
tests exist specifically to pin those down: a budget breach halts even when a
retry is available, and even when verification just passed. A ceiling that
yields to a pending success is not a ceiling — it is a suggestion.

Retries carry the verifier's specific diagnostics forward into the next
attempt. A retry that re-sends the original prompt is a re-roll, which is
paying twice for one draw from the same distribution.

**Changes my mind:** Phase 2 (step 34) replaces fixed retry with adaptive
strategy selection — but it gets A/B tested against this baseline rather than
assumed better, and this module stays as the control arm.

---

## 2026-07-29 — Workspace is durable, containers are ephemeral

**Decision:** The per-task workspace lives on the host disk keyed by task id
and survives crashes. Containers are created and destroyed per activity.

**Why:** A container that had to survive between activities would be a second
kind of recoverable state competing with the event log. Instead a restarted
worker re-creates a container over the same workspace and continues — the same
principle as the event log itself: one source of truth, re-derive the rest.

**Changes my mind:** Container startup cost showing up as a material fraction
of task latency in the Phase 1 benchmark. Measure before optimising.

---

## 2026-07-29 — Test fixtures for the secret scanner are assembled, not literal

**Decision:** Credential fixtures in `tests/test_secrets.py` are built at
runtime via `_fake(prefix, body)`. A test asserts no complete token-shaped
literal exists anywhere in that file.

**Why:** GitHub push protection blocked a push over the Slack fixture. It was
right to: a scanner matches raw file contents and cannot know our value is
fabricated. Every fixture was audited and confirmed dead — AWS's documented
`...EXAMPLE` key, sequential-alphabet fakes, the public jwt.io demo token, a
26-character PEM stub.

This is not evading the control. The control exists to stop real credentials
reaching a public repo, and there were none to stop. The cost of leaving them
as literals is worse than the inconvenience: a repo whose scanner alerts are
all false positives is a repo where alerts get clicked through, and that is how
a real leak eventually ships. Keeping the signal clean is the security
decision here.

The assembled string is byte-identical, so the regex under test is exercised
exactly as before — coverage did not change, only the on-disk representation.

**Operational note:** the flagged content lived in an already-made local commit.
Push protection scans every commit in a push, so fixing the file in a *new*
commit does not clear it — the history had to be rewritten (`git reset --soft
origin/main`, then recommit) before the push would go through.

**Changes my mind:** Nothing. If a future fixture genuinely needs a literal
form, it gets an explicit scanner allowlist entry committed alongside it, so
the exemption is visible in the repo rather than buried in a settings page.

---

## 2026-07-29 — The sandbox needs its own image

**Decision:** `Dockerfile.sandbox` (python:3.12-slim + pytest + ruff + git),
built as `bucker-sandbox:latest`. The image name is config
(`BUCKER_SANDBOX_IMAGE`), not a literal.

**Why:** Discovered while building the smoke run, and it is a genuine
consequence of the isolation design rather than an oversight. Containers run
with `--network none`, so nothing can be installed at task time — every tool a
verifier needs must already be baked in. `python:3.12-slim` has no pytest, so
the verifier could never have run.

The tempting fix — allow network for a `pip install` step — would have quietly
destroyed the containment property that the sandbox tests assert. A sandbox
that pip-installs at runtime is a sandbox with a network. Baking the tools in
keeps `--network none` true and makes the tool set explicit and reviewable.

This also matches how the SWE-bench harness works (prebuilt images per
instance), so it is the right shape for step 26 rather than a detour.

**Changes my mind:** Nothing. Pin by digest instead of tag before publishing
benchmark numbers, so a silent base-image update cannot move results.

---

## 2026-07-29 — Smoke run before the replay engine

**Decision:** Build an end-to-end smoke run (`scripts/smoke_run.py`) before
step 23, and run the components directly rather than through Temporal.

**Why:** Every component was tested against fakes. Nothing had ever run against
a real model, a real container, or real pytest output — so "216 tests pass" said
nothing about whether the pieces *compose*. The sandbox image problem above was
found within minutes of writing this script and would have survived any amount
of additional unit testing.

Bypassing Temporal is deliberate: durability is already proven by M1, and what
was unproven is the AI pipeline. Keeping the orchestrator out means a failure
here has one possible cause instead of two.

The task is deliberately trivial — a missing `subtract` function — so a failure
indicates broken plumbing rather than a hard problem. Difficulty belongs in the
benchmark, where it is the point.

Secondary benefit, and the reason to do it before step 23: the live run creates
the first real recordings, so the replay engine can be tested against genuine
model output instead of hand-written fixtures.

**Changes my mind:** Nothing.

---

## 2026-07-29 — max_tokens is always set, and is part of the request identity

**Decision:** Every model call sends an explicit `max_tokens`, sized per
component (planner 2000, worker 8000, default 4000, all env-overridable). It is
included in the request digest.

**Why:** The first live run failed with a 402 from OpenRouter: *"You requested
up to 64000 tokens, but can only afford 2666."* We were passing `max_tokens=None`,
so the provider assumed the model's maximum. Providers reserve credit against
that ceiling before generating, so a request whose real answer is a few hundred
tokens was rejected for want of credit for 64,000.

The billing failure is the symptom. The actual defect is that this project's
entire thesis is hard ceilings — budget, deadline, retries all enforced — and
the most direct ceiling of all, output length, was left unbounded. An unbounded
generation is an unbounded bill. Sizing it per component costs nothing and is
correct independent of any provider's billing model.

`max_tokens` joins the digest because it can truncate a response: the same
prompt at 500 and at 8000 tokens are genuinely different calls, and replaying
one as the other would misrepresent what happened.

**Changes my mind:** Nothing. If a component legitimately needs more, raise its
own ceiling explicitly rather than removing the concept.

---

## 2026-07-29 — Provider errors get translated, not re-raised raw

**Decision:** `explain_provider_error()` maps 402 / 401 / 404 / rate-limit
responses to a short cause-and-fix, and the smoke run prints that instead of the
provider traceback.

**Why:** The 402 above arrived as roughly eighty lines of nested tracebacks
across three libraries, with the one useful sentence buried in the middle of a
JSON blob. The four realistic causes — no credit, bad key, wrong model name,
rate limit — each have a different fix, and the raw output makes them look
identical. Diagnosis quality is a feature, and every hour lost to an opaque
error is an hour not spent on the benchmark.

Same principle as the `.env` diagnostic added earlier the same day: when a
failure is predictable, say which one it is.

**Changes my mind:** Nothing.

---

## 2026-07-29 — The first real smoke run may use a local model

**Decision:** Treat an Ollama-backed model (`ollama/<model>`) as a valid live
provider for the smoke run. The preflight verifies that its local model is
installed and does not require a cloud API key.

**Why:** The evidence required here is a real model response flowing through
the router, archive, worker, sandbox, verifier, and recording store. It does
not require a billable cloud request. A local model supplies the same boundary
and lets a contributor produce their first real recordings without buying API
credit. Replays remain identical regardless of which provider created them.

**Changes my mind:** Use a hosted provider as the default smoke target if the
benchmark requires a particular hosted model or if local-model output proves
too unreliable for this deliberately tiny task.

---

## <!-- next entry: date, decision, why, what changes my mind -->
