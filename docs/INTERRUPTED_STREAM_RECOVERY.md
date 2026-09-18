# Interrupted-stream recovery — proposed acceptance contract

Status: specification, not an implementation or a claim of passing tests.
Scope: desktop quick-edit loop. Keep strict verification and isolation unchanged.

## Observed baseline

- `bucker/gateway/routing.py:342–376` prohibits candidate fallback after a
  text/tool delta has been forwarded. The gateway does not execute tools.
- `bucker/desktop_gateway/streaming.py:106–125` emits an error frame instead
  of a successful finish on failure; `[DONE]` alone is not proof of success.
- `desktop/src/main/session.ts:12–18` requests session loading when supported.
  Loading history is not proof of recovering an interrupted execution.
- Existing engine tests cover pre-delta fallback and mid-flight failure.
  They do not establish end-to-end tool execution safety or crash recovery.

## Required behavior

| Interruption point | Required outcome | Automatic retry |
|---|---|---|
| Before any output is forwarded | Bounded inference fallback to an eligible candidate; record attempt | Gateway only, within deadline |
| After text or partial tool arguments | Mark attempt interrupted; invalidate its pending tool proposals; keep text visibly incomplete | No |
| Complete tool JSON but no successful response completion | Do not authorize or execute the proposal | No |
| Validated complete response awaiting approval | No execution without approval bound to that proposal | No |
| Execution started, outcome missing | Mark outcome unknown; reconcile evidence before allowing re-execution | No |
| Execution completed and result persisted | Reload the recorded result; do not execute again | No |
| User cancels or app closes | Cancel outstanding generation where possible; invalidate pending approvals; retain known results | No |

A failed stream discards buffered proposals, not arbitrary workspace files.
Earlier tools in the same turn may already have changed files. Never promise
rollback or say “nothing changed” without evidence. Terminating a process is
not proof that its external side effects stopped.

Default message: “This attempt was interrupted. Pending tool proposals were
not run. Earlier actions may have completed; review activity before retrying.”
Use the pending-proposals claim only when the execution gate establishes it.
For uncertain execution: “An action started, but its outcome is unknown. Review
its result before continuing.” Do not display “retrying” unless retry started.

## Completion and execution gate

The agent/tool boundary—not the renderer or inference gateway—must enforce:

1. Valid protocol completion for the tool-producing response; bare EOF,
   `[DONE]`, parseable JSON, timeout, error, and truncated output are insufficient.
2. Complete, schema-valid tool arguments and permitted tool identity.
3. Required user approval bound to session, turn, attempt, proposal, and exact
   arguments. A regenerated proposal cannot reuse an old approval.
4. Durable execution-intent record before dispatch and a result record after it.
5. Reject duplicate dispatch for a known execution ID; reject late events from
   superseded attempts. Do not deduplicate by provider tool-call ID alone.

Proposed correlation fields: `session_id`, `turn_id`, `request_id`, `attempt_id`,
`proposal_id`, `execution_id`, and ordered event sequence. Their persistence and
ownership must be verified in the pinned harness before selecting an implementation.
If the harness cannot enforce these gates, mark this contract blocked; a UI flag
or gateway error alone cannot substitute for enforcement.

Exactly-once arbitrary shell/external effects are not promised. A crash between
an effect and its result record leaves an unknown outcome. Retry requires tool-
specific reconciliation or explicit user review, not blind whole-turn replay.

## Instrumentation required for acceptance

Use monotonic clocks for durations, wall time only for persisted timestamps.
Record request and attempt start, provider/model, first content delta, first tool
argument delta, completion/interruption, approval, dispatch, and result times.
Expose first-content latency and first-tool-delta latency separately; keep request
latency (including fallback) separate from individual provider attempt latency.

Cache-hit/miss and token usage are nullable provider-reported fields with source
and units. Missing usage is unknown, not zero. Record failed attempts too. Do not
log keys, raw prompts, tool arguments, or provider error bodies by default.

## Falsifiable acceptance cases

All cases require correlated event traces and assertions on actual invocation
counts plus workspace/external fixture effects, not screenshots alone.

| ID | Fault or flow | Pass condition |
|---|---|---|
| R1 | Live provider, approved edit and test | Real edit and test result visible; restart restores session identity, transcript, approval/result evidence, and saved file |
| R2 | Primary fails before first delta on a later request in a task | Eligible fallback succeeds; earlier completed action count remains one |
| R3 | Stream breaks after partial tool arguments | Zero dispatches for interrupted proposals, zero proposal-caused writes, no successful finish, no silent fallback |
| R4 | Valid JSON arrives but stream fails before completion | Same zero-dispatch outcome as R3 |
| R5 | Approval denied or stale approval arrives after interruption | Zero dispatches; stale approval rejected |
| R6 | Kill at generation, approval, dispatch, and post-effect/pre-result boundaries | Restart reports accurate interrupted/unknown/completed states; no automatic re-execution |
| R7 | Duplicate/delayed events and repeated retry clicks | One dispatch per approved execution ID; invalid attempts cannot alter current state |
| R8 | First action succeeds; next generation is interrupted | First action remains recorded and is not rolled back or replayed; partial next proposal never runs |
| R9 | Error after a finish frame, before transport closes | Successful completion boundary is explicit and tested; never reinterpret an already dispatched action as safe to replay |
| R10 | Two eligible providers, repeated identical workloads | Per-attempt and request latency recorded; unavailable cache/usage fields remain null; no policy change inferred from one run |

R2–R9 use deterministic provider/transport and side-effect fixtures, including
silent timeouts and connection loss. R1 additionally requires a real provider
through the packaged desktop. Tests must exercise the pinned agent's retry
behavior as well as the gateway; automatic SDK retries must not bypass the gate.

Done means all cases have passing evidence, documented unknown-outcome recovery,
and a restart test of persisted execution evidence. Existing backend crash tests
or a bundled ACP handshake do not satisfy this contract on their own.

## Dependencies and scope limits

Define and test R3–R9 before adding recovery UI promises. Start telemetry with
these tests, validate at least two catalog entries before comparative latency
work, then test onboarding and clean-machine installation against the resulting
flow. Do not speculate tool execution or change routing policy in this slice.
Provider errors such as expired key versus wrong scope must only be distinguished
when upstream evidence supports that distinction; otherwise report the known
category and concrete checks, without inventing a cause.
