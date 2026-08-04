"""Task templates: one-click starting points for common jobs.

A template is a named preset — objective, task type, verifier hint, and
sensible defaults — so a user (or a schedule, or another agent via MCP)
can say "run the research template" instead of writing a full objective
from scratch. Templates are data, not code: adding one is adding a dict,
which keeps the surface reviewable.

The API exposes GET /templates; the new-task form renders them as cards;
schedules resolve an objective from a template by name.
"""

from __future__ import annotations

from typing import Any

#: Registry. Keys are stable public identifiers — renaming one breaks
#: existing schedules, so treat this as an append-only list.
TEMPLATES: dict[str, dict[str, Any]] = {
    "code-fix": {
        "name": "Code fix",
        "description": (
            "Make the failing test suite pass: plan, patch, verify. The "
            "classic bucker workload — objective becomes a contract, a "
            "worker writes a diff in a network-isolated sandbox, the "
            "verifier runs the real tests."
        ),
        "objective": (
            "Inspect the workspace, find what is broken, and fix it so the "
            "project's own test suite passes. Do not change behavior that "
            "tests already cover."
        ),
        "task_type": "code_change",
        "default_budget_usd": 0.25,
        "default_deadline_minutes": 30,
        "default_max_retries": 2,
    },
    "feature-add": {
        "name": "Add a feature",
        "description": (
            "Implement a described feature with tests. The verifier runs "
            "your new tests — if they don't exist, the task cannot pass, "
            "which is the point."
        ),
        "objective": (
            "Implement the requested feature. Write tests for it first; "
            "the task is only verified when those tests pass alongside the "
            "existing suite."
        ),
        "task_type": "code_change",
        "default_budget_usd": 0.50,
        "default_deadline_minutes": 60,
        "default_max_retries": 2,
    },
    "research": {
        "name": "Research with citations",
        "description": (
            "Answer a question with a cited report. The citation verifier "
            "checks every claim's source — no fabricated references survive."
        ),
        "objective": (
            "Research the question and produce a concise cited report. "
            "Every factual claim must carry a source that actually "
            "contains it; the verifier rejects fabricated or mismatched "
            "citations."
        ),
        "task_type": "research",
        "default_budget_usd": 0.40,
        "default_deadline_minutes": 45,
        "default_max_retries": 1,
    },
    "data-extraction": {
        "name": "Structured extraction",
        "description": (
            "Turn unstructured input into a validated schema. The verifier "
            "checks shape, types, and required fields."
        ),
        "objective": (
            "Extract the requested fields into the specified structured "
            "format. Output must satisfy the schema exactly — extra fields, "
            "wrong types, or missing values fail verification."
        ),
        "task_type": "code_change",
        "default_budget_usd": 0.20,
        "default_deadline_minutes": 20,
        "default_max_retries": 2,
    },
    "demo": {
        "name": "Five-step demo",
        "description": (
            "The Phase 0 durability demo: five steps, exactly-once "
            "semantics, crash-proof. No model calls, no sandbox — the "
            "fastest way to see the platform work."
        ),
        "objective": "run the five-step durability demo",
        "task_type": "demo",
        "default_budget_usd": None,
        "default_deadline_minutes": None,
        "default_max_retries": 0,
    },
}

#: Order templates are shown in the UI (registry order is insertion order,
#: but keep the demo last and the common ones first explicitly).
_UI_ORDER = ["code-fix", "feature-add", "research", "data-extraction", "demo"]


class UnknownTemplateError(KeyError):
    """Raised when a schedule or request names a template that doesn't exist."""


def list_templates() -> list[dict]:
    """All templates in UI order, with their objective."""
    out = []
    for key in _UI_ORDER:
        t = TEMPLATES[key]
        out.append({"id": key, **t})
    return out


def resolve_template(name: str) -> dict[str, Any]:
    """Look up a template by id, raising UnknownTemplateError when missing."""
    try:
        return TEMPLATES[name]
    except KeyError:
        known = ", ".join(TEMPLATES)
        raise UnknownTemplateError(
            f"unknown template {name!r} — known templates: {known}"
        ) from None
