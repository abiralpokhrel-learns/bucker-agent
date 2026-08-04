"""Budget and deadline enforcement tests (step 32)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bucker.core.budget import (
    BudgetExceeded,
    BudgetState,
    DeadlineExceeded,
    DeadlineState,
    check_budget,
    check_deadline,
    pre_spend_decision,
)

# -------------------------------------------------------------- BudgetState --


def test_budget_state_under_budget():
    s = BudgetState(cost_usd=0.50, budget_usd=1.00)
    s.check()  # must not raise


def test_budget_state_exactly_at_budget():
    s = BudgetState(cost_usd=1.00, budget_usd=1.00)
    s.check()  # equal is OK (not exceeded)


def test_budget_state_over_budget():
    s = BudgetState(cost_usd=1.01, budget_usd=1.00)
    with pytest.raises(BudgetExceeded) as exc:
        s.check()
    assert "1.0100" in str(exc.value)
    assert "1.0000" in str(exc.value)


def test_budget_state_no_budget():
    """None budget means unlimited."""
    s = BudgetState(cost_usd=9999.99, budget_usd=None)
    s.check()  # must not raise


def test_remaining_at_half():
    s = BudgetState(cost_usd=0.30, budget_usd=1.00)
    assert s.remaining() == 0.70


def test_remaining_when_broke():
    s = BudgetState(cost_usd=2.00, budget_usd=1.00)
    assert s.remaining() == 0.0


# ------------------------------------------------------------- DeadlineState --


def test_deadline_state_under_time():
    now = datetime.now(UTC)
    s = DeadlineState(
        started_at=now - timedelta(minutes=5),
        deadline_minutes=10,
    )
    s.check(now=now)


def test_deadline_state_over_time():
    now = datetime.now(UTC)
    s = DeadlineState(
        started_at=now - timedelta(minutes=15),
        deadline_minutes=10,
    )
    with pytest.raises(DeadlineExceeded):
        s.check(now=now)


def test_deadline_state_no_deadline():
    s = DeadlineState(started_at=datetime.now(UTC), deadline_minutes=None)
    s.check()


# --------------------------------------------------------------- guards ----


def test_check_budget_halts_before_spend():
    """Budget guard checks cost + estimate, not just current."""
    with pytest.raises(BudgetExceeded):
        check_budget(cost_so_far=0.90, budget_usd=1.00, next_step_estimate=0.20)


def test_check_budget_allows_last_call():
    """If estimate fits within remaining, no exception."""
    check_budget(cost_so_far=0.80, budget_usd=1.00, next_step_estimate=0.20)


def test_check_deadline_halts_before_spend():
    now = datetime.now(UTC)
    with pytest.raises(DeadlineExceeded):
        check_deadline(
            started_at=now - timedelta(minutes=9),
            deadline_minutes=10,
            next_step_minutes=2.0,
            now=now,
        )


# ------------------------------------- pre-spend decision (workflow guard) --


def test_pre_spend_allows_under_budget():
    assert pre_spend_decision(0.50, 1.00, 2.0, 10, attempt=1) is None


def test_pre_spend_halts_over_budget_before_work():
    decision = pre_spend_decision(1.10, 1.00, 2.0, 10, attempt=2)
    assert decision is not None
    assert decision["action"] == "halt"
    assert "budget exceeded" in decision["reason"]
    assert "attempt 2" in decision["reason"]


def test_pre_spend_halts_on_deadline_before_work():
    decision = pre_spend_decision(0.10, 1.00, 12.0, 10, attempt=1)
    assert decision is not None
    assert decision["action"] == "halt"
    assert "deadline exceeded" in decision["reason"]


def test_pre_spend_no_budget_never_halts_on_cost():
    # No budget configured: cost can never trigger a halt.
    assert pre_spend_decision(999.0, None, 2.0, 10, attempt=1) is None


def test_pre_spend_no_deadline_never_halts_on_time():
    assert pre_spend_decision(0.10, 1.00, 999.0, None, attempt=1) is None


def test_pre_spend_budget_wins_over_deadline():
    """Both breached: the budget reason is reported (checked first)."""
    decision = pre_spend_decision(2.0, 1.00, 99.0, 10, attempt=3)
    assert decision is not None
    assert "budget exceeded" in decision["reason"]


def test_pre_spend_decision_is_deterministic():
    """Same inputs, same decision — the workflow can replay it safely."""
    a = pre_spend_decision(1.10, 1.00, 12.0, 10, attempt=2)
    b = pre_spend_decision(1.10, 1.00, 12.0, 10, attempt=2)
    assert a == b


def test_pre_spend_reserves_next_step_estimate():
    """The reserve catches the call ABOUT to fire, before it happens.

    cost (0.99) + estimate (0.02) > budget (1.00) -> halt, even though the
    already-spent cost alone would not breach.
    """
    decision = pre_spend_decision(
        0.99, 1.00, 2.0, 10, attempt=1, next_step_estimate=0.02
    )
    assert decision is not None
    assert "estimated" in decision["reason"]


def test_pre_spend_estimate_does_not_fire_when_covered():
    assert pre_spend_decision(
        0.95, 1.00, 2.0, 10, attempt=1, next_step_estimate=0.02
    ) is None  # 0.95 + 0.02 = 0.97 <= 1.00


def test_pre_spend_zero_estimate_checks_spent_only():
    assert pre_spend_decision(0.99, 1.00, 2.0, 10, attempt=1) is None
    assert pre_spend_decision(
        0.99, 1.00, 2.0, 10, attempt=1, next_step_estimate=0.0
    ) is None
