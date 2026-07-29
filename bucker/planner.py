"""Planner: fuzzy objective in, strictly-typed Task contract out.

[HAND] — this is the piece that prevents drift. Everything downstream trusts
that what it receives is schema-valid, so the validation gate here must never
be softened "just this once."

Design decisions worth defending:

  * **One re-prompt, then fail.** Not zero (models fumble JSON, and the repair
    prompt fixes most of it cheaply). Not unlimited (a planner that cannot
    produce a valid contract in two attempts has a prompt problem, and looping
    burns budget while hiding it).
  * **Failures are recorded, never swallowed.** Every invalid attempt becomes a
    ``SchemaValidationFailed`` event. That stream is the raw material for
    improving the prompt later — the schema-failure rate is a quality metric,
    not just a pass/fail gate.
  * **Prompts live in versioned files**, not inline strings. A prompt edit
    changes behaviour as surely as a code edit and deserves the same review.

Pure logic lives here so it can be tested with a fake router; the Temporal
activity wrapper in ``bucker.activities.planner`` adds the event writes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from bucker.config import settings
from bucker.contracts.models import Task, ValidationFailure, validate_task
from bucker.router.client import ModelResponse, ModelRouter

PROMPTS = Path(__file__).parent / "prompts"

#: Verifiers the planner may choose from. Grows as verifiers are registered.
KNOWN_VERIFIERS = ("python_test_runner", "noop")


class PlanningFailed(Exception):
    """The planner could not produce a valid contract within its attempts."""

    def __init__(self, attempts: list[PlanAttempt]) -> None:
        self.attempts = attempts
        detail = "; ".join(
            f"attempt {i + 1}: {a.errors}" for i, a in enumerate(attempts)
        )
        super().__init__(f"planner produced no valid contract ({detail})")


@dataclass(slots=True)
class PlanAttempt:
    """One try at producing a contract — valid or not, it is recorded."""

    raw_text: str
    response: ModelResponse
    errors: list[str] = field(default_factory=list)
    task: Task | None = None

    @property
    def ok(self) -> bool:
        return self.task is not None


@dataclass(slots=True)
class PlanResult:
    task: Task
    attempts: list[PlanAttempt]

    @property
    def cost_usd(self) -> float:
        return round(sum(a.response.cost_usd for a in self.attempts), 6)

    @property
    def repaired(self) -> bool:
        """True when the first attempt failed validation and the retry saved it."""
        return len(self.attempts) > 1


# --------------------------------------------------------------- parsing ----
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def extract_json(text: str) -> dict:
    """Pull one JSON object out of a model response.

    Models wrap JSON in markdown fences or add a sentence of preamble even when
    told not to. Stripping that is not "being lenient about the contract" — the
    contract is about the *content* of the object, which is still validated in
    full. Failing on a stray fence would just burn a retry on a formatting tic.
    """
    cleaned = _FENCE.sub("", text).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back to the outermost {...} span.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("no JSON object found in response") from None
        try:
            parsed = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON in response: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _validate(raw_text: str) -> tuple[Task | None, list[str]]:
    try:
        data = extract_json(raw_text)
    except ValueError as exc:
        return None, [str(exc)]

    try:
        return validate_task(data), []
    except ValidationFailure as exc:
        return None, exc.errors
    except Exception as exc:  # pydantic or anything unexpected
        return None, [f"{type(exc).__name__}: {exc}"]


# -------------------------------------------------------------- prompting ---
def _render(filename: str, **values: object) -> str:
    """Render a prompt template.

    ``string.Template`` ($name), not ``str.format`` ({name}): these prompts are
    mostly JSON schema, and ``format`` treats every brace in that schema as a
    placeholder and raises. Learned the hard way — the planner test suite caught
    it on the first run. ``safe_substitute`` so an unknown $token in a prompt is
    left alone rather than crashing a live task.
    """
    template = Template((PROMPTS / filename).read_text(encoding="utf-8"))
    return template.safe_substitute(**values)


def build_prompt(objective: str, *, verifiers: tuple[str, ...] = KNOWN_VERIFIERS) -> str:
    return _render(
        "planner_v1.md",
        objective=objective,
        verifiers=", ".join(verifiers),
        default_budget_usd=settings.default_budget_usd,
        default_deadline_minutes=settings.default_deadline_minutes,
    )


def build_repair_prompt(previous: str, errors: list[str]) -> str:
    return _render(
        "planner_v1_repair.md",
        previous=previous,
        errors="\n".join(f"- {e}" for e in errors),
    )


# ----------------------------------------------------------------- planner --
async def generate_task_contract(
    router: ModelRouter,
    objective: str,
    *,
    verifiers: tuple[str, ...] = KNOWN_VERIFIERS,
    max_attempts: int = 2,
) -> PlanResult:
    """Turn an objective into a validated Task, or raise ``PlanningFailed``.

    Returns every attempt, including failed ones, so the caller can record each
    as an event. The caller — not this function — writes to the event log; that
    keeps this testable without a database.
    """
    attempts: list[PlanAttempt] = []
    messages = [{"role": "user", "content": build_prompt(objective, verifiers=verifiers)}]

    for attempt_no in range(1, max_attempts + 1):
        response = await router.complete(messages, purpose="planner")
        task, errors = _validate(response.text)

        attempt = PlanAttempt(
            raw_text=response.text, response=response, errors=errors, task=task
        )
        attempts.append(attempt)

        if task is not None:
            return PlanResult(task=task, attempts=attempts)

        if attempt_no < max_attempts:
            # One repair round, with the specific errors fed back. Vague
            # "try again" prompts produce vague corrections.
            messages = [
                {
                    "role": "user",
                    "content": build_repair_prompt(response.text, errors),
                }
            ]

    raise PlanningFailed(attempts)
