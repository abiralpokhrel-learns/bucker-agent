"""Adaptive planning (BUILD_PLAN step 34).

[HAND] — on repeated failure, vary strategy rather than re-rolling the same
prompt. This is A/B tested against the fixed-retry baseline; it must not be
assumed to be better, only measured.

Strategies:
  1. DEFAULT — replay the original plan with failure context (fixed retry).
  2. CHUNK — break the objective into smaller sub-tasks.
  3. CLARIFY — ask a clarifying question about the objective.
  4. SWITCH_MODEL — use a different model for the next attempt.

The selector reads the failure pattern (which verifier failed, how many times,
what the diagnostics say) and picks a strategy. The fixed baseline is always
available as a comparison point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class Strategy(StrEnum):
    """What to do differently on the next attempt."""

    DEFAULT = "default"          # Re-run with failure context (fixed retry).
    CHUNK = "chunk"              # Break the objective into sub-tasks.
    CLARIFY = "clarify"          # Ask a clarifying question.
    SWITCH_MODEL = "switch_model"  # Use a different model.


#: Fallback models to try when SWITCH_MODEL is selected. Ordered by cost
#: (cheapest first) so the system tries cheaper alternatives before burning
#: budget on a frontier model.
FALLBACK_MODELS = (
    "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/minimax/minimax-m3",
    "openrouter/anthropic/claude-sonnet-4",
)


@dataclass(slots=True)
class AttemptHistory:
    """What we know about the failures so far."""

    attempt: int
    verifier_name: str = ""
    diagnostics: list[str] = field(default_factory=list)
    passed: list[bool] = field(default_factory=list)
    models_used: list[str] = field(default_factory=list)

    @property
    def consecutive_failures(self) -> int:
        count = 0
        for p in reversed(self.passed):
            if not p:
                count += 1
            else:
                break
        return count

    @property
    def all_same_model(self) -> bool:
        return len(set(self.models_used)) <= 1

    def has_pattern(self, keyword: str) -> bool:
        """True if any diagnostic contains the keyword."""
        return any(keyword.lower() in d.lower() for d in self.diagnostics)


# ------------------------------------------------------------ selector ----


def choose_strategy(history: AttemptHistory) -> Strategy:
    """Pick a strategy based on the failure pattern.

    The decision tree:
      1st failure → DEFAULT (re-prompt with diagnostics).
      2nd failure, same model → SWITCH_MODEL.
      2nd failure, already switched, broad failure → CHUNK.
      3rd failure → CLARIFY (escalate ambiguity to the user).

    This is deliberately simple. The Phase 2 evaluation (M3) measures
    whether this reduces repeat-failure rate vs fixed retry.
    """
    if history.consecutive_failures == 0:
        return Strategy.DEFAULT

    if history.consecutive_failures == 1:
        return Strategy.DEFAULT

    if history.consecutive_failures >= 3:
        return Strategy.CLARIFY

    # 2 consecutive failures.
    if history.all_same_model:
        return Strategy.SWITCH_MODEL

    # Already switched models once and still failing — try chunking.
    if history.has_pattern("import") or history.has_pattern("no tests"):
        return Strategy.CHUNK

    return Strategy.SWITCH_MODEL


# ------------------------------------------------------- plan modifiers ----


def chunk_objective(objective: str, diagnostics: list[str]) -> str:
    """Break the objective into smaller sub-tasks based on diagnostics.

    When the verifier reports 'ImportError' or 'no tests collected',
    the task is likely too ambiguous. Chunking adds structure.
    """
    diag_text = "; ".join(diagnostics[-3:]) if diagnostics else "unknown failure"

    return (
        f"{objective}\n\n"
        f"The previous attempt failed with: {diag_text}\n\n"
        f"Break this down into smaller steps:\n"
        f"1. First, identify what specific change is needed by reading the "
        f"   workspace files and the test output above.\n"
        f"2. Produce a minimal diff that fixes ONE specific test failure.\n"
        f"3. Do not refactor or restructure — only the minimal change.\n"
        f"4. Stop and report done after the first fix. Let the verifier "
        f"decide if more work is needed."
    )


def clarify_objective(objective: str, diagnostics: list[str]) -> str:
    """Ask a clarifying question when repeated failures suggest ambiguity."""
    diag_summary = "; ".join(diagnostics[-2:]) if diagnostics else "repeated failure"

    return (
        f"The task objective was:\n\n{objective}\n\n"
        f"It has failed {len(diagnostics)} times with symptoms like: "
        f"{diag_summary}\n\n"
        f"Stop producing diffs. Instead, ask ONE specific clarifying question "
        f"about the objective — something that, if answered, would resolve "
        f"the ambiguity. Return this as your response with done=true."
    )


def next_model(current: str, used: list[str]) -> str:
    """Pick the next model to try. Avoids models already tried."""
    tried = set(used)
    for model in FALLBACK_MODELS:
        if model not in tried:
            return model
    return current  # fallback: same model
