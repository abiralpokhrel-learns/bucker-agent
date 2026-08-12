"""State reconstruction tests (step 7).

Table-driven: an event list in, an exact state out. These are the tests that
catch a fold handler quietly doing the wrong thing, which is the failure mode
that would poison every guarantee built on top of the event log.
"""

from __future__ import annotations

import pytest

from bucker.core.events import EventType
from bucker.core.state import UnknownEventType, initial_state, rebuild_state
from tests.conftest import make_event


def test_empty_stream_gives_initial_state():
    assert rebuild_state([]) == initial_state()


def test_task_created_populates_identity():
    events = [
        make_event(1, EventType.TASK_CREATED, {
            "objective": "add jwt auth",
            "task_type": "code_change",
            "verifier": "python_test_runner",
            "budget_usd": 0.75,
        })
    ]
    state = rebuild_state(events)
    assert state["objective"] == "add jwt auth"
    assert state["task_type"] == "code_change"
    assert state["verifier"] == "python_test_runner"
    assert state["budget_usd"] == 0.75
    assert state["status"] == "pending"
    assert state["last_event_id"] == 1


def test_happy_path_sequence():
    events = [
        make_event(1, EventType.TASK_CREATED, {"objective": "x" * 10}),
        make_event(2, EventType.TASK_STARTED),
        make_event(3, EventType.PLAN_GENERATED, {"plan": {"steps": ["a", "b"]}}),
        make_event(4, EventType.STEP_COMPLETED, {"step": "a"}),
        make_event(5, EventType.STEP_COMPLETED, {"step": "b"}),
        make_event(6, EventType.VERIFICATION_PASSED, {"verifier": "noop"}),
        make_event(7, EventType.TASK_COMPLETED),
    ]
    state = rebuild_state(events)
    assert state["status"] == "completed"
    assert state["steps_completed"] == ["a", "b"]
    assert state["plan"] == {"steps": ["a", "b"]}
    assert state["last_verification"]["passed"] is True
    assert state["last_event_id"] == 7


def test_retry_loop_counts_attempts_and_escalates():
    events = [
        make_event(1, EventType.TASK_CREATED, {"objective": "y" * 10}),
        make_event(2, EventType.VERIFICATION_FAILED, {"diagnostics": "1 failed"}),
        make_event(3, EventType.RETRY_SCHEDULED, {"attempt": 1}),
        make_event(4, EventType.VERIFICATION_FAILED, {"diagnostics": "1 failed"}),
        make_event(5, EventType.RETRY_SCHEDULED, {"attempt": 2}),
        make_event(6, EventType.VERIFICATION_FAILED, {"diagnostics": "1 failed"}),
        make_event(7, EventType.NEEDS_HUMAN_REVIEW, {"reason": "max retries"}),
    ]
    state = rebuild_state(events)
    assert state["attempts"] == 2
    assert state["status"] == "needs_human_review"
    assert state["halted_reason"] == "max retries"
    assert state["last_verification"]["passed"] is False


def test_cost_accumulates_across_model_calls():
    events = [
        make_event(1, EventType.TASK_CREATED, {"objective": "z" * 10}),
        make_event(2, EventType.MODEL_CALL_COMPLETED, {"cost_usd": 0.01}),
        make_event(3, EventType.MODEL_CALL_COMPLETED, {"cost_usd": 0.025}),
        make_event(4, EventType.MODEL_CALL_COMPLETED, {"cost_usd": 0.0005}),
    ]
    assert rebuild_state(events)["cost_usd"] == pytest.approx(0.0355)


def test_budget_exceeded_halts():
    events = [
        make_event(1, EventType.TASK_CREATED, {"objective": "q" * 10}),
        make_event(2, EventType.MODEL_CALL_COMPLETED, {"cost_usd": 0.80}),
        make_event(3, EventType.BUDGET_EXCEEDED, {"budget_usd": 0.75}),
    ]
    state = rebuild_state(events)
    assert state["status"] == "halted"
    assert state["halted_reason"] == "budget_exceeded"


def test_unknown_event_type_raises_never_skips():
    """The strictness rule. Silently ignoring history is the cardinal sin."""
    events = [make_event(1, "SomethingNobodyImplemented", {})]
    with pytest.raises(UnknownEventType, match="SomethingNobodyImplemented"):
        rebuild_state(events)


def test_fold_is_pure_input_not_mutated():
    base = initial_state()
    base_copy = dict(base)
    events = [make_event(1, EventType.TASK_STARTED)]
    rebuild_state(events, base=base)
    assert base == base_copy, "rebuild_state must not mutate the base state"


def test_fold_is_deterministic():
    events = [
        make_event(1, EventType.TASK_CREATED, {"objective": "d" * 10}),
        make_event(2, EventType.STEP_COMPLETED, {"step": "one"}),
        make_event(3, EventType.MODEL_CALL_COMPLETED, {"cost_usd": 0.5}),
    ]
    assert rebuild_state(events) == rebuild_state(events)


def test_correction_event_is_the_only_way_to_change_the_past():
    events = [
        make_event(1, EventType.TASK_CREATED, {"objective": "wrong objective"}),
        make_event(2, EventType.CORRECTION_APPLIED,
                   {"set": {"objective": "right objective"}}),
    ]
    assert rebuild_state(events)["objective"] == "right objective"


def test_every_event_type_has_a_handler():
    """Guardrail: adding an EventType without a fold handler fails here,
    not in production three weeks later."""
    from bucker.core.state import HANDLERS

    missing = [e.value for e in EventType if e.value not in HANDLERS]
    assert not missing, f"EventTypes without fold handlers: {missing}"


# ---------------------------------------------------------------- graphs --

def _graph_events(final_status: str = "completed", failed: list[str] | None = None):
    """A realistic graph-task stream: created, bookends, per-step events.

    Mirrors what bucker/activities/graph.py's record_graph_step emits —
    including the __graph__ bookends whose detail carries the failure list.
    """
    failed = failed or []
    return [
        make_event(1, EventType.TASK_CREATED, {
            "objective": "graph: calc-refactor-demo (3 steps)",
            "task_type": "graph",
            "verifier": None,
        }),
        make_event(2, EventType.GRAPH_STEP_COMPLETED, {
            "step_id": "__graph__", "status": "started",
            "detail": {"name": "calc-refactor-demo", "steps": 3},
        }),
        make_event(3, EventType.GRAPH_STEP_COMPLETED, {
            "step_id": "add-sub", "status": "completed", "detail": {"task_id": "1"},
        }),
        make_event(4, EventType.GRAPH_STEP_COMPLETED, {
            "step_id": "add-mul", "status": "completed", "detail": {"task_id": "2"},
        }),
        make_event(5, EventType.GRAPH_STEP_COMPLETED, {
            "step_id": "verify-both", "status": "completed", "detail": {"task_id": "3"},
        }),
        make_event(6, EventType.GRAPH_STEP_COMPLETED, {
            "step_id": "__graph__", "status": final_status,
            "detail": {"steps": 3, "failed": failed},
        }),
    ]


def test_graph_task_reaches_terminal_status_when_all_steps_pass():
    """The graph task must NOT stay pending: a clean run folds to completed."""
    state = rebuild_state(_graph_events())
    assert state["status"] == "completed"
    assert len(state["graph_steps"]) == 5


def test_graph_task_fails_when_any_step_failed():
    """A graph whose steps failed is a FAILED graph task, even though the
    run itself finished — status must report the outcome."""
    state = rebuild_state(_graph_events(failed=["add-mul"]))
    assert state["status"] == "failed"
    assert state["graph_summary"]["detail"]["failed"] == ["add-mul"]


def test_graph_task_failed_bookend_folds_to_failed():
    """The early-exit bookend (parse/validation error) folds to failed."""
    state = rebuild_state(_graph_events(final_status="failed"))
    assert state["status"] == "failed"


def test_graph_task_in_progress_during_run():
    """The started bookend moves the task from pending to in_progress."""
    state = rebuild_state(_graph_events()[:2])
    assert state["status"] == "in_progress"


def test_per_step_graph_events_do_not_change_status():
    """Per-step events are informational; status only moves on bookends."""
    events = _graph_events()[:5]  # created + started + 3 steps, no final bookend
    state = rebuild_state(events)
    assert state["status"] == "in_progress"  # still running, not completed
