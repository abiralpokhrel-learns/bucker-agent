"""Typed task contract tests (step 13).

Fixture suite of valid/invalid Tasks. When the planner starts emitting real
contracts these are the cases it must satisfy.
"""

from __future__ import annotations

import pytest

from bucker.contracts.models import Task, ValidationFailure, validate_task

VALID = {
    "schema_version": 1,
    "task_type": "code_change",
    "objective": "Add JWT authentication to the API",
    "files": ["auth.py", "middleware.py"],
    "constraints": {"tests_required": True, "coverage": 90},
    "budget_usd": 0.75,
    "deadline_minutes": 15,
    "verifier": "python_test_runner",
}


def test_valid_contract_parses():
    task = validate_task(VALID)
    assert isinstance(task, Task)
    assert task.verifier == "python_test_runner"
    assert task.constraints.coverage == 90


def test_minimal_contract_parses():
    task = validate_task({
        "schema_version": 1,
        "task_type": "demo",
        "objective": "run the demo workflow",
        "verifier": "noop",
    })
    assert task.files == []
    assert task.constraints.tests_required is True


@pytest.mark.parametrize("mutation,expect", [
    ({"objective": "short"}, "objective"),
    ({"task_type": "telepathy"}, "task_type"),
    ({"budget_usd": -1}, "budget_usd"),
    ({"deadline_minutes": 0}, "deadline_minutes"),
    ({"verifier": ""}, "verifier"),
    ({"schema_version": 99}, "schema_version"),
])
def test_invalid_contracts_rejected(mutation, expect):
    data = {**VALID, **mutation}
    with pytest.raises(ValidationFailure) as exc:
        validate_task(data)
    assert expect in str(exc.value)


@pytest.mark.parametrize("missing", ["task_type", "objective", "verifier"])
def test_required_fields(missing):
    data = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ValidationFailure):
        validate_task(data)


def test_unknown_field_rejected():
    """additionalProperties:false — a planner hallucinating a field is a
    validation failure, not a silently ignored key."""
    with pytest.raises(ValidationFailure):
        validate_task({**VALID, "vibes": "immaculate"})


def test_errors_are_actionable():
    """Failure messages get fed back to the planner for its one re-prompt,
    so they must name the offending field."""
    with pytest.raises(ValidationFailure) as exc:
        validate_task({**VALID, "objective": "no"})
    assert "objective" in str(exc.value)
