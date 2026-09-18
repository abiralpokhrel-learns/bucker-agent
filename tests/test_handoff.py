"""Handoff tests: portable swap state from the event log, no second system.

A provider swap is a crash with a different cause: the next model must
reconstruct "where did we stop, repeat or continue" from the log — never
from a raw transcript, and never by resuming a half-generated response.
"""

from __future__ import annotations

from bucker.core import handoff
from bucker.core.events import EventType
from bucker.core.handoff import (
    COMPLETED_UNCONFIRMED,
    IN_FLIGHT,
    NEVER_STARTED,
    advise_switch,
    build_handoff,
    classify_interruption,
)
from bucker.core.state import rebuild_state
from tests.conftest import make_event


def _t(*types_and_payloads):
    """Build a same-task stream from (event_type, payload, [idempotency_key])."""
    from uuid import uuid4

    tid = uuid4()
    out = []
    for i, item in enumerate(types_and_payloads, start=1):
        etype, payload = item[0], item[1]
        key = item[2] if len(item) > 2 else None
        out.append(
            make_event(
                i, etype, payload, task_id=tid, idempotency_key=key,
            )
        )
    return out


# ------------------------------------------------------- state tracking ----
def test_tool_started_marks_pending_and_completed_clears_it():
    key = "t:work-1-apply"
    started = _t(
        (EventType.TOOL_CALL_STARTED,
         {"tool": "apply_diff", "attempt": 1, "completion_key": key}, "t:started"),
    )
    state = rebuild_state(started)
    assert state["pending_tools"][key]["status"] == "in_flight"

    done = _t(
        (EventType.TOOL_CALL_STARTED,
         {"tool": "apply_diff", "attempt": 1, "completion_key": key}, "t:started"),
        (EventType.TOOL_CALL_COMPLETED, {"tool": "apply_diff", "exit_code": 0}, key),
    )
    assert rebuild_state(done)["pending_tools"] == {}


# ------------------------------------------------------- classification ----
def test_empty_stream_is_at_rest_and_safe():
    pending = classify_interruption([])
    assert (pending.kind, pending.status) == ("none", NEVER_STARTED)
    advice = advise_switch(pending)
    assert advice.safe is True and advice.action == "next-turn-anywhere"


def test_tool_in_flight_is_mid_turn_unsafe():
    events = _t(
        (EventType.TOOL_CALL_STARTED,
         {"tool": "apply_diff", "attempt": 1, "completion_key": "k"}, "s"),
    )
    pending = classify_interruption(events)
    assert (pending.kind, pending.status) == ("tool", IN_FLIGHT)
    advice = advise_switch(pending)
    assert advice.safe is False
    assert advice.action == "discard-partial-retry-whole-turn"


def test_tool_done_without_worker_close_is_unconfirmed():
    events = _t(
        (EventType.TOOL_CALL_STARTED,
         {"tool": "apply_diff", "attempt": 1, "completion_key": "k"}, "s"),
        (EventType.TOOL_CALL_COMPLETED, {"tool": "apply_diff", "exit_code": 0}, "k"),
    )
    pending = classify_interruption(events)
    assert pending.status == COMPLETED_UNCONFIRMED
    assert advise_switch(pending).action == "discard-partial-retry-whole-turn"


def test_worker_without_verdict_is_unconfirmed():
    events = _t(
        (EventType.WORKER_COMPLETED,
         {"attempt": 1, "status": "produced", "summary": "did x"}, "w1"),
    )
    pending = classify_interruption(events)
    assert (pending.kind, pending.status) == ("model", COMPLETED_UNCONFIRMED)


def test_verification_requested_without_verdict():
    events = _t(
        (EventType.WORKER_COMPLETED,
         {"attempt": 1, "status": "produced", "summary": "did x"}, "w1"),
        (EventType.VERIFICATION_REQUESTED, {"verifier": "v", "attempt": 1}, "r1"),
    )
    pending = classify_interruption(events)
    assert pending.kind == "verification"
    assert pending.status in (IN_FLIGHT, COMPLETED_UNCONFIRMED)


def test_retry_with_no_next_model_call_is_safe_boundary():
    events = _t(
        (EventType.WORKER_COMPLETED,
         {"attempt": 1, "status": "produced", "summary": "x"}, "w1"),
        (EventType.VERIFICATION_FAILED,
         {"attempt": 1, "diagnostics": "boom"}, None),
        (EventType.RETRY_SCHEDULED, {"attempt": 1}, None),
    )
    pending = classify_interruption(events)
    assert pending.status == NEVER_STARTED
    advice = advise_switch(pending)
    assert advice.safe is True
    assert advice.point == "turn-boundary"


def test_failed_verdict_is_a_boundary_not_mid_turn():
    events = _t(
        (EventType.WORKER_COMPLETED,
         {"attempt": 1, "status": "produced", "summary": "x"}, "w1"),
        (EventType.VERIFICATION_FAILED,
         {"attempt": 1, "diagnostics": "boom"}, None),
    )
    assert advise_switch(classify_interruption(events)).safe is True


# ------------------------------------------------------- handoff object ----
def test_handoff_note_is_bounded_and_explicit():
    events = _t(
        (EventType.WORKER_COMPLETED,
         {"attempt": 1, "status": "produced", "summary": "added sub"}, "w1"),
        (EventType.VERIFICATION_FAILED,
         {"attempt": 1, "diagnostics": "test_sub failed"}, None),
        (EventType.RETRY_SCHEDULED, {"attempt": 1}, None),
    )
    state = build_handoff(
        events, task_id="t1", objective="Add sub to calc.py",
        verifier="python_test_runner", models_tried=("a/m1",),
    )
    assert state.objective == "Add sub to calc.py"
    assert state.last_verdict == "failed"
    assert len(state.attempts) == 1 and state.attempts[0].verified is False
    assert len(state.note) <= handoff.MAX_HANDOFF_NOTE_CHARS
    for section in ("OBJECTIVE", "DONE SO FAR", "LAST VERDICT",
                    "INTERRUPTION", "NEXT"):
        assert section in state.note


def test_handoff_compacts_accumulated_objective():
    acc = "Fix login\n\nAttempt 1 failed verification.\nOld tail."
    state = build_handoff([], task_id="t", objective=acc)
    assert state.objective == "Fix login"
    assert "Old tail" not in state.note


def test_handoff_carries_no_provider_routing():
    state = build_handoff([], task_id="t", objective="do x")
    assert state.models_tried == ()
    assert state.switch.action in (
        "next-turn-anywhere", "retry-same-request-on-B",
        "discard-partial-retry-whole-turn",
    )


# ------------------------------------------------------- idempotent apply ----
async def test_pre_apply_prior_result_skips_duplicate_side_effect(tmp_path):
    """A retry after 'tool ran, result unconfirmed' must not re-apply."""
    import json

    from bucker.contracts.models import Task
    from bucker.router.client import ModelResponse
    from bucker.sandbox.runtime import DockerSandbox, ExecResult
    from bucker.worker_agent import execute_task

    produced = {
        "schema_version": 1, "status": "produced", "summary": "s",
        "diff": "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-a\n+b\n",
        "files_touched": ["calc.py"],
    }

    class FakeRouter:
        model = "fake-model"
        mode = "recorded"

        def __init__(self):
            self.calls = 0

        async def complete(self, messages, *, purpose, **kwargs):
            self.calls += 1
            return ModelResponse(
                text=json.dumps(produced), model=self.model, cost_usd=0.0,
                latency_ms=1, raw_ref="sha256:" + "0" * 64,
                request_ref="sha256:" + "1" * 64, from_recording=True,
            )

    ws = tmp_path / "ws"
    ws.mkdir()
    sandbox = DockerSandbox(ws)
    sandbox.write_file("calc.py", "a\n")

    prior = ExecResult(
        command="apply_diff", exit_code=0, stdout="prior",
        stderr="", duration_ms=0, timed_out=False, secret_findings=[],
    )
    seen = {}

    async def pre_apply(result):
        seen["called"] = True
        return prior

    outcome = await execute_task(
        FakeRouter(), Task(
            schema_version=1, task_type="code_change",
            objective="Add a subtract function to calc.py",
            files=["calc.py"], verifier="python_test_runner",
            budget_usd=0.5, deadline_minutes=10,
        ),
        sandbox, apply=True, pre_apply=pre_apply,
    )
    assert seen.get("called") is True
    assert outcome.applied is prior
    # Workspace untouched by a second apply.
    assert sandbox.read_file("calc.py") == "a\n"
