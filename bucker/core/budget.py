"""Budget and deadline enforcement (BUILD_PLAN step 32).

[HAND] — ceilings that yield to retries are not ceilings. This module
provides the enforcement checks that halt a task *before* the next spend,
not just after the fact. It runs as a synchronous check in the workflow
and as a pre-activity guard.

Design:
  - Enforcement is synchronous (no I/O) so it can run in Temporal workflow
    code where determinism rules apply.
  - The actual cost/time values come from activities (impure); the guard
    just compares them against the limits.
  - Breach is terminal — no appeal path, no override. The platform's
    whole design argument is that ceilings are real.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class BudgetExceeded(Exception):
    """Raised when cumulative cost exceeds the task's budget."""

    def __init__(self, spent: float, budget: float) -> None:
        self.spent = spent
        self.budget = budget
        super().__init__(
            f"budget exceeded: spent ${spent:.4f} of ${budget:.4f}"
        )


class DeadlineExceeded(Exception):
    """Raised when elapsed time exceeds the task's deadline."""

    def __init__(self, elapsed_minutes: float, deadline_minutes: int) -> None:
        self.elapsed = elapsed_minutes
        self.deadline = deadline_minutes
        super().__init__(
            f"deadline exceeded: {elapsed_minutes:.1f}min of "
            f"{deadline_minutes}min"
        )


@dataclass(frozen=True, slots=True)
class BudgetState:
    """Snapshot of spend for enforcement checks."""

    cost_usd: float = 0.0
    budget_usd: float | None = None

    def check(self) -> None:
        """Raise BudgetExceeded if over budget."""
        if self.budget_usd is not None and self.cost_usd > self.budget_usd:
            raise BudgetExceeded(self.cost_usd, self.budget_usd)

    def remaining(self) -> float:
        if self.budget_usd is None:
            return float("inf")
        return max(0.0, self.budget_usd - self.cost_usd)


@dataclass(frozen=True, slots=True)
class DeadlineState:
    """Snapshot of time for enforcement checks."""

    started_at: datetime | None = None
    deadline_minutes: int | None = None

    def check(self, now: datetime | None = None) -> None:
        """Raise DeadlineExceeded if over deadline."""
        if self.started_at is None or self.deadline_minutes is None:
            return
        now = now or datetime.now()
        elapsed = (now - self.started_at).total_seconds() / 60.0
        if elapsed > self.deadline_minutes:
            raise DeadlineExceeded(elapsed, self.deadline_minutes)

    def elapsed_minutes(self, now: datetime | None = None) -> float:
        if self.started_at is None:
            return 0.0
        now = now or datetime.now()
        return (now - self.started_at).total_seconds() / 60.0


# -------------------------------------------------------- workflow guard ----


def pre_spend_decision(
    cost_so_far: float,
    budget_usd: float | None,
    elapsed_minutes: float,
    deadline_minutes: int | None,
    attempt: int,
    *,
    next_step_estimate: float = 0.0,
) -> dict | None:
    """Pure guard: the decision to halt BEFORE the next model spend.

    Returns a policy-shaped HALT dict when the next step is not allowed,
    else None. Deterministic by design — elapsed time is an ARGUMENT, so a
    Temporal workflow can feed it ``workflow.now()`` (the wall clock is
    forbidden in workflow code). ``check_budget``/``check_deadline`` above
    are the same rules for non-workflow contexts; this one is the version
    that is safe to call inside a workflow.

    ``next_step_estimate`` is the caller's guess at what the upcoming model
    call will cost (e.g. a conservative per-call reserve). It is NOT a
    charge — the check is ``cost_so_far + estimate > budget`` — so a single
    expensive call cannot blow past the limit; the halt happens while the
    budget would still cover the call, or the reserve is conservative
    enough that the overrun is bounded by one call's cost. Pass 0.0 to
    check only what has already been spent.

    The decision dict matches what ``evaluate_policy`` returns for a HALT,
    so the workflow can hand it straight to ``record_decision``.
    """
    if budget_usd is not None and cost_so_far + next_step_estimate > budget_usd:
        return {
            "action": "halt",
            "reason": (
                f"budget exceeded before attempt {attempt}: spent "
                f"${cost_so_far:.4f} of ${budget_usd:.4f}"
                + (
                    f" (next call estimated at ${next_step_estimate:.4f})"
                    if next_step_estimate
                    else ""
                )
            ),
            "failure_context": "",
        }
    if deadline_minutes is not None and elapsed_minutes > deadline_minutes:
        return {
            "action": "halt",
            "reason": (
                f"deadline exceeded before attempt {attempt}: "
                f"{elapsed_minutes:.1f}min of {deadline_minutes}min"
            ),
            "failure_context": "",
        }
    return None


def check_budget(
    cost_so_far: float,
    budget_usd: float | None,
    *,
    next_step_estimate: float = 0.0,
) -> None:
    """Halt if the next step would breach the budget.

    Called before every expensive activity (model call, sandbox exec).
    Checks whether cost_so_far + estimate would exceed the limit, and
    raises BudgetExceeded before the spend, not after.
    """
    state = BudgetState(
        cost_usd=cost_so_far + next_step_estimate,
        budget_usd=budget_usd,
    )
    state.check()


def check_deadline(
    started_at: datetime | None,
    deadline_minutes: int | None,
    *,
    next_step_minutes: float = 1.0,
    now: datetime | None = None,
) -> None:
    """Halt if the next step would breach the deadline."""
    if started_at is None or deadline_minutes is None:
        return
    now = now or datetime.now()
    elapsed = (now - started_at).total_seconds() / 60.0
    if elapsed + next_step_minutes > deadline_minutes:
        raise DeadlineExceeded(elapsed, deadline_minutes)
