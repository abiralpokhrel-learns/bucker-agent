"""Retry and escalation policy tests (step 22).

Pure decision logic, so every branch is cheap to pin down — including the
precedence rules, which is where policies like this usually go wrong.
"""

from __future__ import annotations

import pytest

from bucker.retry import Action, AttemptState, decide


def state(**kwargs) -> AttemptState:
    defaults = dict(attempt=1, max_retries=2, verification_passed=False)
    return AttemptState(**{**defaults, **kwargs})


# ------------------------------------------------------------ happy path ----
def test_passing_verification_completes():
    d = decide(state(verification_passed=True))
    assert d.action is Action.COMPLETE
    assert d.is_terminal


def test_completion_carries_no_failure_context():
    assert decide(state(verification_passed=True)).failure_context == ""


# ----------------------------------------------------------------- retry ----
def test_first_failure_retries():
    d = decide(state(attempt=1))
    assert d.action is Action.RETRY
    assert not d.is_terminal


def test_retry_carries_the_diagnostics_forward():
    """A retry without the failure is a re-roll — paying twice for one draw."""
    d = decide(state(attempt=1, diagnostics="FAILED tests/test_calc.py::test_sub"))
    assert "test_sub" in d.failure_context
    assert "Fix the specific failures" in d.failure_context


def test_failure_context_is_bounded():
    """Unbounded context blows the budget the retry is meant to conserve."""
    d = decide(state(attempt=1, diagnostics="x" * 50_000))
    assert len(d.failure_context) < 4000


def test_retries_up_to_the_limit():
    assert decide(state(attempt=1, max_retries=2)).action is Action.RETRY
    assert decide(state(attempt=2, max_retries=2)).action is Action.RETRY


# ------------------------------------------------------------- escalation ---
def test_escalates_after_max_retries():
    d = decide(state(attempt=3, max_retries=2))
    assert d.action is Action.ESCALATE
    assert d.is_terminal


def test_escalation_is_not_silent_discard():
    """NeedsHumanReview is the appeal path, so the reason must be legible."""
    d = decide(state(attempt=3, max_retries=2))
    assert "human review" in d.reason


def test_zero_retries_escalates_immediately():
    assert decide(state(attempt=1, max_retries=0)).action is Action.ESCALATE


# --------------------------------------------------------------- ceilings ---
def test_budget_breach_halts():
    d = decide(state(cost_usd=1.20, budget_usd=0.75))
    assert d.action is Action.HALT
    assert "budget" in d.reason


def test_deadline_breach_halts():
    d = decide(state(elapsed_minutes=22.0, deadline_minutes=15))
    assert d.action is Action.HALT
    assert "deadline" in d.reason


def test_budget_outranks_a_pending_retry():
    """A ceiling that yields to a retry is not a ceiling."""
    d = decide(state(attempt=1, max_retries=5, cost_usd=2.0, budget_usd=0.5))
    assert d.action is Action.HALT


def test_budget_outranks_success():
    """A task must not overspend its way to a pass and have it forgiven."""
    d = decide(state(verification_passed=True, cost_usd=2.0, budget_usd=0.5))
    assert d.action is Action.HALT


def test_exactly_at_budget_is_allowed():
    """Breach means over, not equal — the ceiling is spendable."""
    d = decide(state(verification_passed=True, cost_usd=0.75, budget_usd=0.75))
    assert d.action is Action.COMPLETE


def test_exactly_at_deadline_is_allowed():
    d = decide(state(verification_passed=True, elapsed_minutes=15.0,
                     deadline_minutes=15))
    assert d.action is Action.COMPLETE


def test_no_ceilings_set_means_no_halt():
    d = decide(state(verification_passed=True, cost_usd=999.0))
    assert d.action is Action.COMPLETE


# ------------------------------------------------------------- full cycle ---
def test_realistic_failing_task_reaches_a_human():
    """Three failed verifications must land on a person, not loop or vanish."""
    actions = []
    for attempt in range(1, 5):
        d = decide(state(attempt=attempt, max_retries=2, diagnostics="boom"))
        actions.append(d.action)
        if d.is_terminal:
            break

    assert actions == [Action.RETRY, Action.RETRY, Action.ESCALATE]


def test_realistic_recovering_task_completes():
    first = decide(state(attempt=1, diagnostics="1 failed"))
    assert first.action is Action.RETRY

    second = decide(state(attempt=2, verification_passed=True))
    assert second.action is Action.COMPLETE


@pytest.mark.parametrize("attempt", range(1, 8))
def test_always_reaches_a_terminal_state_eventually(attempt):
    """No configuration may produce an infinite loop."""
    d = decide(state(attempt=attempt, max_retries=2))
    assert d.action in (Action.RETRY, Action.ESCALATE)
    if attempt > 2:
        assert d.is_terminal
