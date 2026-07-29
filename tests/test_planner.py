"""Planner tests (step 16).

Driven by a fake router that returns scripted responses, so every branch —
clean success, repair-then-success, exhausted attempts, garbage output — is
provable without a model, a network, or a database.
"""

from __future__ import annotations

import json

import pytest

from bucker.planner import (
    PlanningFailed,
    build_prompt,
    extract_json,
    generate_task_contract,
)
from bucker.router.client import ModelResponse

VALID = {
    "schema_version": 1,
    "task_type": "code_change",
    "objective": "Add JWT authentication to the API",
    "files": ["auth.py"],
    "constraints": {"tests_required": True},
    "budget_usd": 0.75,
    "deadline_minutes": 15,
    "verifier": "python_test_runner",
}


class FakeRouter:
    """Returns scripted responses in order and counts calls."""

    def __init__(self, responses: list[str], *, cost: float = 0.01) -> None:
        self._responses = list(responses)
        self._cost = cost
        self.calls: list[list[dict]] = []
        self.model = "fake-model"
        self.mode = "recorded"

    async def complete(self, messages, *, purpose, **kwargs) -> ModelResponse:
        self.calls.append(messages)
        text = self._responses.pop(0)
        return ModelResponse(
            text=text,
            model=self.model,
            cost_usd=self._cost,
            latency_ms=10,
            raw_ref="sha256:" + "0" * 64,
            request_ref="sha256:" + "1" * 64,
            from_recording=True,
        )


# ------------------------------------------------------------ json parsing --
def test_extract_plain_json():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_from_markdown_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_with_preamble():
    """Models add a sentence even when told not to. Don't burn a retry on it."""
    assert extract_json('Here is the contract:\n{"a": 1}') == {"a": 1}


def test_extract_rejects_non_object():
    with pytest.raises(ValueError, match="expected a JSON object"):
        extract_json("[1, 2, 3]")


def test_extract_rejects_garbage():
    with pytest.raises(ValueError):
        extract_json("I cannot help with that.")


# ------------------------------------------------------------------ happy ---
async def test_valid_contract_first_try():
    router = FakeRouter([json.dumps(VALID)])
    result = await generate_task_contract(router, "add jwt auth")

    assert result.task.verifier == "python_test_runner"
    assert len(result.attempts) == 1
    assert result.repaired is False
    assert len(router.calls) == 1


async def test_objective_reaches_the_prompt():
    router = FakeRouter([json.dumps(VALID)])
    await generate_task_contract(router, "make the login page not crash")
    assert "make the login page not crash" in router.calls[0][0]["content"]


async def test_prompt_lists_available_verifiers():
    prompt = build_prompt("x" * 20, verifiers=("python_test_runner", "noop"))
    assert "python_test_runner" in prompt
    assert "noop" in prompt


# ---------------------------------------------------------------- repair ----
async def test_invalid_then_repaired():
    """The single re-prompt earns its keep on a fixable mistake."""
    broken = json.dumps({**VALID, "objective": "no"})   # too short
    router = FakeRouter([broken, json.dumps(VALID)])

    result = await generate_task_contract(router, "add jwt auth")

    assert result.task.objective == VALID["objective"]
    assert len(result.attempts) == 2
    assert result.repaired is True
    assert result.attempts[0].ok is False
    assert any("objective" in e for e in result.attempts[0].errors)


async def test_repair_prompt_names_the_errors():
    """Vague 'try again' produces vague corrections — feed back specifics."""
    router = FakeRouter([json.dumps({**VALID, "task_type": "telepathy"}),
                         json.dumps(VALID)])
    await generate_task_contract(router, "do a thing")

    repair = router.calls[1][0]["content"]
    assert "task_type" in repair
    assert "telepathy" in repair


async def test_garbage_then_valid():
    router = FakeRouter(["I'm sorry, I can't do that.", json.dumps(VALID)])
    result = await generate_task_contract(router, "add jwt auth")
    assert result.repaired is True


# ------------------------------------------------------------------ fail ----
async def test_two_failures_gives_up():
    """Not unlimited retries — a planner that can't produce valid JSON twice
    has a prompt problem, and looping hides it while burning budget."""
    bad = json.dumps({**VALID, "verifier": ""})
    router = FakeRouter([bad, bad])

    with pytest.raises(PlanningFailed) as exc:
        await generate_task_contract(router, "add jwt auth")

    assert len(exc.value.attempts) == 2
    assert len(router.calls) == 2, "must not call the model a third time"


async def test_failed_attempts_are_preserved_for_the_event_log():
    """The failures are the training signal — they must survive the exception."""
    bad = json.dumps({**VALID, "budget_usd": -5})
    router = FakeRouter([bad, bad])

    with pytest.raises(PlanningFailed) as exc:
        await generate_task_contract(router, "x" * 20)

    for attempt in exc.value.attempts:
        assert attempt.errors
        assert attempt.raw_text == bad
        assert attempt.response.raw_ref.startswith("sha256:")


async def test_unknown_field_is_rejected():
    """additionalProperties:false — a hallucinated key must not slip through."""
    router = FakeRouter([json.dumps({**VALID, "vibes": "great"}),
                         json.dumps({**VALID, "vibes": "great"})])
    with pytest.raises(PlanningFailed):
        await generate_task_contract(router, "x" * 20)


# ------------------------------------------------------------------ cost ----
async def test_cost_accumulates_across_attempts():
    router = FakeRouter([json.dumps({**VALID, "objective": "no"}),
                         json.dumps(VALID)], cost=0.02)
    result = await generate_task_contract(router, "add jwt auth")
    assert result.cost_usd == pytest.approx(0.04), "repair attempts cost money too"


async def test_max_attempts_is_configurable():
    bad = json.dumps({**VALID, "verifier": ""})
    router = FakeRouter([bad, bad, bad, json.dumps(VALID)])
    result = await generate_task_contract(router, "x" * 20, max_attempts=4)
    assert len(result.attempts) == 4
