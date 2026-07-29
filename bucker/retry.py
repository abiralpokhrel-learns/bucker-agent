"""Retry and escalation policy (step 22).

[HAND] — pure decision logic, no I/O, so every branch is unit-testable. The
workflow calls ``decide()`` and executes whatever comes back; it holds no
policy of its own.

The shape of the policy is the argument:

  * **Retries carry the failure forward.** A retry that re-sends the original
    prompt is a re-roll, and a re-roll is just paying twice for the same
    distribution. The verifier's diagnostics go back to the planner so the next
    attempt is a correction.
  * **Retries are bounded, and the bound is low.** Two by default. A task that
    fails three objective checks has a problem the loop cannot fix, and burning
    budget on attempt seven only delays the human who was always going to be
    needed.
  * **Escalation is a real destination, not an error.** ``NeedsHumanReview`` is
    the appeal path from the Ethical AI Assessment — work that cannot be
    verified is routed to a person, never silently discarded and never forced
    through as if it passed.
  * **Budget and deadline outrank retries.** A breach halts immediately, even
    with attempts remaining. A ceiling that yields to a retry is not a ceiling.

This is fixed policy by design. Adaptive strategy selection — switch model,
chunk smaller, ask for clarification — is Phase 2 (step 34), and it gets
A/B tested against this baseline rather than assumed to be better.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Action(StrEnum):
    COMPLETE = "complete"
    RETRY = "retry"
    ESCALATE = "escalate"
    HALT = "halt"


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str
    #: Fed back into the planner on RETRY. Empty for every other action.
    failure_context: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.action in (Action.COMPLETE, Action.ESCALATE, Action.HALT)


@dataclass(frozen=True, slots=True)
class AttemptState:
    """Everything the policy is allowed to look at."""

    attempt: int                     # 1-based: this was the Nth attempt
    max_retries: int
    verification_passed: bool
    diagnostics: str = ""
    cost_usd: float = 0.0
    budget_usd: float | None = None
    elapsed_minutes: float = 0.0
    deadline_minutes: int | None = None


def decide(state: AttemptState) -> Decision:
    """Decide what happens after one verification round."""

    # Ceilings first, unconditionally. Checked before success so a task cannot
    # overspend its way to a pass and have the breach quietly forgiven.
    if state.budget_usd is not None and state.cost_usd > state.budget_usd:
        return Decision(
            Action.HALT,
            f"budget exceeded: spent ${state.cost_usd:.4f} of ${state.budget_usd:.4f}",
        )

    if (
        state.deadline_minutes is not None
        and state.elapsed_minutes > state.deadline_minutes
    ):
        return Decision(
            Action.HALT,
            f"deadline exceeded: {state.elapsed_minutes:.1f}min of "
            f"{state.deadline_minutes}min",
        )

    if state.verification_passed:
        return Decision(Action.COMPLETE, "verification passed")

    if state.attempt <= state.max_retries:
        return Decision(
            Action.RETRY,
            f"verification failed, attempt {state.attempt} of "
            f"{state.max_retries + 1}",
            failure_context=_build_failure_context(state),
        )

    return Decision(
        Action.ESCALATE,
        f"verification failed {state.attempt} times, escalating to human review",
    )


def _build_failure_context(state: AttemptState) -> str:
    """What the planner sees on the next attempt.

    Specific and bounded. Vague context produces vague corrections; unbounded
    context blows the budget the retry is supposed to be conserving.
    """
    return (
        f"Attempt {state.attempt} failed verification.\n\n"
        f"Verifier diagnostics:\n{state.diagnostics[:3000]}\n\n"
        f"Fix the specific failures above. Do not restate the original plan."
    )
