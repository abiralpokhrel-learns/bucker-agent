"""Typed task contracts (step 13).

The Planner's output is validated here before it is allowed to become an event.
A malformed contract is recorded as ``SchemaValidationFailed`` and re-prompted
once — never silently dropped, because those failures are the training signal
for better prompts later.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_PATH = Path(__file__).parent / "task.schema.json"


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    return Draft202012Validator(schema)


class ValidationFailure(Exception):
    """Raised when a proposed Task does not satisfy the contract."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class Constraints(BaseModel):
    model_config = ConfigDict(extra="allow")

    tests_required: bool = True
    coverage: float | None = None
    max_diff_lines: int | None = None


class Task(BaseModel):
    """A validated unit of work. Mirrors task.schema.json — keep them in sync."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    task_type: str
    objective: str = Field(min_length=8, max_length=2000)
    files: list[str] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    budget_usd: float | None = None
    deadline_minutes: int | None = None
    verifier: str
    parent_id: str | None = None


def validate_task(data: dict[str, Any]) -> Task:
    """Validate against the JSON Schema, then build the typed model.

    Both layers on purpose: the JSON Schema is the language-neutral published
    contract (other implementations can validate against it), pydantic is the
    ergonomic in-process type.
    """
    errors = [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in sorted(_validator().iter_errors(data), key=lambda e: list(e.path))
    ]
    if errors:
        raise ValidationFailure(errors)
    return Task(**data)


def is_valid(data: dict[str, Any]) -> bool:
    try:
        validate_task(data)
        return True
    except (ValidationFailure, Exception):
        return False
