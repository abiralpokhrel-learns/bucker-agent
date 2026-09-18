"""Portable task-state handoff for provider swaps (and crashes).

What this is NOT: KV-cache preservation. A provider's computational state is
proprietary to its infrastructure and tied to its weights — it cannot be
handed from one provider to another even in principle. Model B always pays a
full cold prefill, and there is a small real latency blip at the switch.
"Seamless" would oversell it.

What this IS: enough LOGICAL task state that model B does not rediscover
what model A already knew — built from the same append-only event log that
crash recovery already uses (a swap is structurally a crash: something
interrupted, and the next worker must reconstruct enough state to continue
correctly). No second tracking system.

The handoff object is provider-agnostic on purpose:

  * normalized turn history (the gateway already normalizes deltas and
    tool_calls into one canonical shape; the log stores outcomes, never
    provider payloads)
  * plan/objective as a stable base (see bucker.core.context), not a raw
    transcript
  * which attempts produced what (worker summaries + verification verdicts)
  * exactly where the interruption happened: never-started | in-flight |
    completed-unconfirmed — the field that decides "repeat or continue"

Safe switch points (mirrors the gateway's streaming rule — fallback only
before the first forwarded delta, never mid-stream):

  * before any output for a turn went out → retry the same request on B
  * at a completed turn boundary (verdict recorded) → next turn anywhere
  * mid-turn interruption → discard the partial output and retry the WHOLE
    turn on B. Model B must never "finish model A's sentence".

All logic here is pure (events in, handoff out) so it is unit-testable and
safe to call from workflow code, activities, the lite runner, or the API.
"""

from __future__ import annotations

from dataclasses import dataclass

from bucker.core.context import base_objective
from bucker.core.events import EventType
from bucker.core.eventstore import Event

#: Interruption taxonomy — the one field that matters for "repeat or skip".
NEVER_STARTED = "never-started"
IN_FLIGHT = "in-flight"
COMPLETED_UNCONFIRMED = "completed-unconfirmed"

#: Switch advice points.
TURN_BOUNDARY = "turn-boundary"
MID_TURN = "mid-turn"

#: Ceiling for the rendered handoff note. Short on purpose: a weak model
#: orients better from an explicit digest than from a long noisy transcript,
#: and the note rides along as prefill on a cold model.
MAX_HANDOFF_NOTE_CHARS = 1500


@dataclass(frozen=True, slots=True)
class AttemptSummary:
    """One worker turn, as the log remembers it."""

    attempt: int
    status: str | None          # produced | blocked | no_change_needed | None
    summary: str                # worker's own claim (unverified prose)
    verified: bool | None       # True / False / None (no verdict yet)


@dataclass(frozen=True, slots=True)
class PendingAction:
    """Where the interruption happened, exactly."""

    kind: str                   # model | tool | verification | none
    status: str                 # never-started | in-flight | completed-unconfirmed
    detail: str                 # human-legible, bounded


@dataclass(frozen=True, slots=True)
class SwitchAdvice:
    """Whether model B may take over right now, and how."""

    safe: bool
    point: str                  # turn-boundary | mid-turn
    action: str                 # retry-same-request-on-B | next-turn-anywhere |
                                # discard-partial-retry-whole-turn


@dataclass(frozen=True, slots=True)
class HandoffState:
    """Everything portable that model B needs. No provider ids required to
    interpret it (models_tried is diagnostics, not routing)."""

    task_id: str
    objective: str              # stable base, byte-identical across retries
    verifier: str | None
    attempts: tuple[AttemptSummary, ...]
    last_verdict: str | None    # passed | failed | None
    pending: PendingAction
    switch: SwitchAdvice
    models_tried: tuple[str, ...] = ()
    note: str = ""              # rendered handoff note (render_handoff_note)


def _event_types(events: list[Event]) -> list[str]:
    return [str(e.event_type) for e in events]


def classify_interruption(events: list[Event]) -> PendingAction:
    """Locate the interruption from the event tail (newest-first scan).

    Order of checks matters: the LATEST unconfirmed step wins, because an
    older confirmed step is already settled history.
    """
    by_type = _event_types(events)

    def _latest(*types: str) -> int:
        for i in range(len(events) - 1, -1, -1):
            if by_type[i] in types:
                return i
        return -1

    i_tool_start = _latest(str(EventType.TOOL_CALL_STARTED))
    i_tool_done = _latest(str(EventType.TOOL_CALL_COMPLETED))
    if i_tool_start > i_tool_done:
        return PendingAction(
            kind="tool",
            status=IN_FLIGHT,
            detail="tool started with no recorded result",
        )

    i_worker = _latest(str(EventType.WORKER_COMPLETED))
    irq = _latest(str(EventType.VERIFICATION_REQUESTED))
    ipass = _latest(str(EventType.VERIFICATION_PASSED))
    ifail = _latest(str(EventType.VERIFICATION_FAILED))
    iverdict = max(ipass, ifail)

    if irq > iverdict:
        # Verification was requested but no verdict recorded. If a worker
        # result exists for this attempt the work is done but unconfirmed;
        # otherwise the verification itself is in flight.
        if i_worker > iverdict:
            return PendingAction(
                kind="verification",
                status=COMPLETED_UNCONFIRMED,
                detail="worker result recorded, verification verdict missing",
            )
        return PendingAction(
            kind="verification",
            status=IN_FLIGHT,
            detail="verification requested, no verdict recorded",
        )

    if i_tool_done > i_worker and i_tool_done > iverdict:
        # Tool result recorded but the worker turn never closed.
        return PendingAction(
            kind="tool",
            status=COMPLETED_UNCONFIRMED,
            detail="tool result recorded, worker turn not closed",
        )

    if i_worker > iverdict:
        return PendingAction(
            kind="model",
            status=COMPLETED_UNCONFIRMED,
            detail="worker result recorded, verification verdict missing",
        )

    i_retry = _latest(
        str(EventType.RETRY_SCHEDULED), str(EventType.BUDGET_EXCEEDED),
        str(EventType.DEADLINE_EXCEEDED),
    )
    i_model_call = _latest(str(EventType.MODEL_CALL_COMPLETED))
    if i_retry > i_model_call and i_retry > iverdict:
        return PendingAction(
            kind="model",
            status=NEVER_STARTED,
            detail="retry scheduled, next attempt has no model call yet",
        )

    return PendingAction(kind="none", status=NEVER_STARTED, detail="at rest")


def advise_switch(pending: PendingAction) -> SwitchAdvice:
    """Map the interruption to the safe switch point.

    Turn boundary (nothing half-finished): B may retry the same pending
    request or take the next turn — nothing to discard. Mid-turn: B must
    discard any partial output and retry the WHOLE turn; resuming a
    half-generated response on a different model is incoherent.
    """
    if pending.kind == "none":
        return SwitchAdvice(
            safe=True, point=TURN_BOUNDARY, action="next-turn-anywhere"
        )
    if pending.status == NEVER_STARTED:
        return SwitchAdvice(
            safe=True, point=TURN_BOUNDARY, action="retry-same-request-on-B"
        )
    return SwitchAdvice(
        safe=False, point=MID_TURN, action="discard-partial-retry-whole-turn"
    )


def _attempts_from_events(events: list[Event]) -> list[AttemptSummary]:
    attempts: list[AttemptSummary] = []
    verdict_by_attempt: dict[int, bool] = {}
    for e in events:
        t = str(e.event_type)
        if t == str(EventType.VERIFICATION_PASSED):
            a = e.payload.get("attempt")
            if isinstance(a, int):
                verdict_by_attempt[a] = True
        elif t == str(EventType.VERIFICATION_FAILED):
            a = e.payload.get("attempt")
            if isinstance(a, int):
                verdict_by_attempt[a] = False
    for e in events:
        if str(e.event_type) != str(EventType.WORKER_COMPLETED):
            continue
        a = e.payload.get("attempt")
        if not isinstance(a, int):
            continue
        attempts.append(AttemptSummary(
            attempt=a,
            status=e.payload.get("status"),
            summary=str(e.payload.get("summary", ""))[:300],
            verified=verdict_by_attempt.get(a),
        ))
    attempts.sort(key=lambda x: x.attempt)
    return attempts


def render_handoff_note(
    *,
    objective: str,
    attempts: list[AttemptSummary],
    last_verdict: str | None,
    pending: PendingAction,
    switch: SwitchAdvice,
    max_chars: int = MAX_HANDOFF_NOTE_CHARS,
) -> str:
    """Short explicit digest: plan, what's done, last action+result, what's
    next. Bounded; the tail (most actionable part) is preserved on cut."""
    lines = [f"OBJECTIVE: {base_objective(objective).strip()[:800]}"]
    if attempts:
        done = "; ".join(
            f"attempt {a.attempt}: {a.status or '?'}"
            + (
                " (verified)"
                if a.verified is True
                else " (failed verification)"
                if a.verified is False
                else " (unverified)"
            )
            for a in attempts[-3:]
        )
        lines.append(f"DONE SO FAR: {done}")
        last = attempts[-1]
        if last.summary:
            lines.append(f"LAST RESULT: {last.summary[:400]}")
    else:
        lines.append("DONE SO FAR: nothing produced yet")
    lines.append(f"LAST VERDICT: {last_verdict or 'none yet'}")
    lines.append(f"INTERRUPTION: {pending.kind} / {pending.status} ({pending.detail})")
    lines.append(
        f"NEXT: {switch.action}"
        + ("" if switch.safe else " — do not treat any partial output as valid")
    )
    note = "\n".join(lines)
    if len(note) <= max_chars:
        return note
    # Preserve the actionable tail (interruption + next), cut the middle.
    tail = "\n".join(lines[-3:])
    head_room = max_chars - len(tail) - len("\n...\n")
    return lines[0][:max(0, head_room)] + "\n...\n" + tail


def build_handoff(
    events: list[Event],
    *,
    task_id: str,
    objective: str,
    verifier: str | None = None,
    models_tried: tuple[str, ...] | list[str] = (),
) -> HandoffState:
    """Fold an event stream into the portable handoff for model B.

    Pure: events + task snapshot in, provider-agnostic state out. File
    CONTENTS are deliberately not included — the per-task workspace on disk
    is the source of truth for those; this object orients, it does not
    transport. Pair it with the compacted base objective (not the raw
    transcript) so the cold prefill on B stays small.
    """
    attempts = _attempts_from_events(events)
    last_verdict: str | None = None
    for e in reversed(events):
        t = str(e.event_type)
        if t == str(EventType.VERIFICATION_PASSED):
            last_verdict = "passed"
            break
        if t == str(EventType.VERIFICATION_FAILED):
            last_verdict = "failed"
            break
    pending = classify_interruption(events)
    switch = advise_switch(pending)
    note = render_handoff_note(
        objective=objective,
        attempts=attempts,
        last_verdict=last_verdict,
        pending=pending,
        switch=switch,
    )
    return HandoffState(
        task_id=task_id,
        objective=base_objective(objective),
        verifier=verifier,
        attempts=tuple(attempts),
        last_verdict=last_verdict,
        pending=pending,
        switch=switch,
        models_tried=tuple(models_tried),
        note=note,
    )
