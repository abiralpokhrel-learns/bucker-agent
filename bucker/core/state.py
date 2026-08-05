"""State reconstruction: events -> current state.

[HAND] — a pure fold, and it must stay pure. No I/O, no clock reads, no
randomness. Given the same events it returns the same state, on any machine,
in any year. That property is what makes crash recovery and replay trustworthy.

The strictness rule: an unknown event type raises ``UnknownEventType``. It is
tempting to ignore unrecognised events so old streams keep working. Do not. A
silently skipped event means reconstructed state quietly diverges from what
actually happened, which is the exact failure this architecture exists to
prevent. Add a handler, or add a version branch.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from bucker.core.events import EventType
from bucker.core.eventstore import Event


class UnknownEventType(Exception):
    """Raised when folding an event no handler knows about."""


State = dict[str, Any]
Handler = Callable[[State, Event], None]


def initial_state() -> State:
    """The empty state every stream folds up from."""
    return {
        "status": "unknown",
        "objective": None,
        "task_type": None,
        "verifier": None,
        "budget_usd": None,
        "plan": None,
        "steps_completed": [],
        "artifacts": {},          # logical name -> blob ref
        "attempts": 0,
        "cost_usd": 0.0,
        "last_verification": None,
        "last_event_id": 0,
        "halted_reason": None,
    }


# --------------------------------------------------------------- handlers ---
# One per EventType. Each mutates state in place; the fold owns the copying.

def _task_created(s: State, e: Event) -> None:
    p = e.payload
    s["status"] = "pending"
    s["objective"] = p.get("objective")
    s["task_type"] = p.get("task_type")
    s["verifier"] = p.get("verifier")
    s["budget_usd"] = p.get("budget_usd")


def _task_started(s: State, e: Event) -> None:
    s["status"] = "in_progress"


def _plan_requested(s: State, e: Event) -> None:
    s["status"] = "in_progress"


def _plan_generated(s: State, e: Event) -> None:
    s["plan"] = e.payload.get("plan")
    s["status"] = "in_progress"


def _schema_validation_failed(s: State, e: Event) -> None:
    # Recorded, never fatal on its own — the planner gets one re-prompt.
    s.setdefault("schema_failures", []).append(
        {"event_id": e.id, "errors": e.payload.get("errors")}
    )


def _step_started(s: State, e: Event) -> None:
    s["status"] = "in_progress"


def _step_completed(s: State, e: Event) -> None:
    name = e.payload.get("step")
    if name is not None:
        s["steps_completed"].append(name)


def _tool_call_completed(s: State, e: Event) -> None:
    if e.tool_output_ref:
        s["artifacts"][f"tool:{e.id}"] = e.tool_output_ref


def _model_call_completed(s: State, e: Event) -> None:
    s["cost_usd"] = round(s["cost_usd"] + float(e.payload.get("cost_usd") or 0.0), 6)
    if e.tool_output_ref:
        s["artifacts"][f"model:{e.id}"] = e.tool_output_ref


def _model_call_failed(s: State, e: Event) -> None:
    s.setdefault("model_failures", []).append({"event_id": e.id, **e.payload})


def _worker_completed(s: State, e: Event) -> None:
    s["artifacts"]["worker_result"] = e.tool_output_ref or e.payload.get("result_ref")
    s["status"] = "in_progress"


def _verification_requested(s: State, e: Event) -> None:
    s["status"] = "in_progress"


def _verification_passed(s: State, e: Event) -> None:
    s["last_verification"] = {"passed": True, "event_id": e.id, **e.payload}


def _verification_failed(s: State, e: Event) -> None:
    s["last_verification"] = {"passed": False, "event_id": e.id, **e.payload}
    s["status"] = "verification_failed"


def _retry_scheduled(s: State, e: Event) -> None:
    s["attempts"] += 1
    s["status"] = "in_progress"


def _needs_human_review(s: State, e: Event) -> None:
    s["status"] = "needs_human_review"
    s["halted_reason"] = e.payload.get("reason", "repeated verification failure")


def _task_completed(s: State, e: Event) -> None:
    s["status"] = "completed"


def _task_failed(s: State, e: Event) -> None:
    s["status"] = "failed"
    s["halted_reason"] = e.payload.get("reason")


def _budget_exceeded(s: State, e: Event) -> None:
    s["status"] = "halted"
    s["halted_reason"] = "budget_exceeded"


def _deadline_exceeded(s: State, e: Event) -> None:
    s["status"] = "halted"
    s["halted_reason"] = "deadline_exceeded"


def _correction_applied(s: State, e: Event) -> None:
    # The one sanctioned way to change the past: a later event that overrides
    # specific keys. Explicit, ordered, and itself part of the audit trail.
    for key, value in (e.payload.get("set") or {}).items():
        s[key] = value


def _redaction_applied(s: State, e: Event) -> None:
    for ref in e.payload.get("refs") or []:
        for key, value in list(s["artifacts"].items()):
            if value == ref:
                s["artifacts"][key] = "<redacted>"


def _critique_completed(s: State, e: Event) -> None:
    """Informational: the self-critique verdict. Does not change task status."""
    s["critiques"] = s.get("critiques", 0) + 1
    if e.payload.get("verdict") == "needs_fix":
        s["repairs"] = s.get("repairs", 0) + int(bool(e.payload.get("repaired")))


def _graph_step_completed(s: State, e: Event) -> None:
    """Informational: one DAG step finished. Does not change task status."""
    steps = s.setdefault("graph_steps", [])
    steps.append({
        "step_id": e.payload.get("step_id"),
        "status": e.payload.get("status"),
    })


def _human_approved(s: State, e: Event) -> None:
    """A human accepted the escalated result — terminal, honest status."""
    s["status"] = "human_approved"
    s["review_note"] = e.payload.get("note", "")


def _human_rejected(s: State, e: Event) -> None:
    """A human rejected the escalated result — terminal, honest status."""
    s["status"] = "human_rejected"
    s["review_note"] = e.payload.get("note", "")


HANDLERS: dict[str, Handler] = {
    EventType.TASK_CREATED: _task_created,
    EventType.TASK_STARTED: _task_started,
    EventType.TASK_COMPLETED: _task_completed,
    EventType.TASK_FAILED: _task_failed,
    EventType.PLAN_REQUESTED: _plan_requested,
    EventType.PLAN_GENERATED: _plan_generated,
    EventType.SCHEMA_VALIDATION_FAILED: _schema_validation_failed,
    EventType.STEP_STARTED: _step_started,
    EventType.STEP_COMPLETED: _step_completed,
    EventType.TOOL_CALL_COMPLETED: _tool_call_completed,
    EventType.MODEL_CALL_COMPLETED: _model_call_completed,
    EventType.MODEL_CALL_FAILED: _model_call_failed,
    EventType.CRITIQUE_COMPLETED: _critique_completed,
    EventType.GRAPH_STEP_COMPLETED: _graph_step_completed,
    EventType.HUMAN_APPROVED: _human_approved,
    EventType.HUMAN_REJECTED: _human_rejected,
    EventType.WORKER_COMPLETED: _worker_completed,
    EventType.VERIFICATION_REQUESTED: _verification_requested,
    EventType.VERIFICATION_PASSED: _verification_passed,
    EventType.VERIFICATION_FAILED: _verification_failed,
    EventType.RETRY_SCHEDULED: _retry_scheduled,
    EventType.NEEDS_HUMAN_REVIEW: _needs_human_review,
    EventType.BUDGET_EXCEEDED: _budget_exceeded,
    EventType.DEADLINE_EXCEEDED: _deadline_exceeded,
    EventType.CORRECTION_APPLIED: _correction_applied,
    EventType.REDACTION_APPLIED: _redaction_applied,
}


# ------------------------------------------------------------------- fold ---
def rebuild_state(events: list[Event], base: State | None = None) -> State:
    """Fold events into state, starting from ``base`` (or empty state).

    ``base`` is how snapshots plug in: pass the snapshot's state and only the
    events after it. The result must be identical to folding the whole stream —
    that invariant is property-tested in tests/test_snapshots.py.
    """
    state = deepcopy(base) if base is not None else initial_state()

    for event in events:
        handler = HANDLERS.get(event.event_type)
        if handler is None:
            raise UnknownEventType(
                f"No fold handler for event_type={event.event_type!r} "
                f"(event id={event.id}). Add one in bucker/core/state.py — "
                f"never skip unknown events."
            )
        handler(state, event)
        state["last_event_id"] = event.id

    return state
