"""Graph engineering: multi-step task DAGs (bucker/contracts/graph.py).

A graph is a directed acyclic graph of steps. Each step is a FULL verified
pipeline (planner -> worker -> verifier, with its own budget/retries) run
as a child workflow; steps with no dependency on each other run IN
PARALLEL (Temporal child workflows via asyncio.gather); a step starts only
after every step it depends on has finished. The graph is one task in the
event store (task_type="graph"), so the audit trail covers the whole run.

This module is PURE: spec validation + deterministic topological waves.
No I/O — the workflow and API call into it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class GraphStep:
    id: str
    objective: str
    depends_on: list[str] = field(default_factory=list)
    task_type: str = "code_change"
    verifier: str = "python_test_runner"
    budget_usd: float | None = None
    deadline_minutes: int | None = None
    max_retries: int = 2

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "objective": self.objective,
            "depends_on": list(self.depends_on),
            "task_type": self.task_type,
            "verifier": self.verifier,
            "budget_usd": self.budget_usd,
            "deadline_minutes": self.deadline_minutes,
            "max_retries": self.max_retries,
        }


@dataclass(slots=True)
class GraphSpec:
    name: str
    steps: list[GraphStep] = field(default_factory=list)
    budget_usd: float | None = None      # optional whole-graph ceiling
    fail_fast: bool = False              # stop scheduling after a failed step

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "steps": [s.as_dict() for s in self.steps],
            "budget_usd": self.budget_usd,
            "fail_fast": self.fail_fast,
        }


def parse_spec(data: dict[str, Any]) -> GraphSpec:
    """Build a GraphSpec from JSON. Raises ValueError on malformed input."""
    if not isinstance(data, dict):
        raise ValueError("graph spec must be a JSON object")
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("graph spec needs a 'name'")
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError("graph spec needs a non-empty 'steps' list")

    steps: list[GraphStep] = []
    for raw in steps_raw:
        if not isinstance(raw, dict):
            raise ValueError("each step must be an object")
        sid = str(raw.get("id", "")).strip()
        objective = str(raw.get("objective", "")).strip()
        if not sid:
            raise ValueError("each step needs an 'id'")
        if not objective:
            raise ValueError(f"step {sid!r} needs an 'objective'")
        deps = raw.get("depends_on") or []
        if not isinstance(deps, list):
            raise ValueError(f"step {sid!r}: depends_on must be a list")
        budget = raw.get("budget_usd")
        deadline = raw.get("deadline_minutes")
        retries = raw.get("max_retries", 2)
        steps.append(GraphStep(
            id=sid,
            objective=objective,
            depends_on=[str(d).strip() for d in deps],
            task_type=str(raw.get("task_type", "code_change")),
            verifier=str(raw.get("verifier", "python_test_runner")),
            budget_usd=float(budget) if budget is not None else None,
            deadline_minutes=int(deadline) if deadline is not None else None,
            max_retries=int(retries),
        ))

    budget = data.get("budget_usd")
    return GraphSpec(
        name=name,
        steps=steps,
        budget_usd=float(budget) if budget is not None else None,
        fail_fast=bool(data.get("fail_fast", False)),
    )


def validate_graph(spec: GraphSpec) -> list[str]:
    """Errors that make a graph unrunnable. Empty list = valid."""
    errors: list[str] = []
    ids = [s.id for s in spec.steps]
    if len(ids) != len(set(ids)):
        dupes = {i for i in ids if ids.count(i) > 1}
        errors.append(f"duplicate step ids: {sorted(dupes)}")

    for s in spec.steps:
        for dep in s.depends_on:
            if dep not in ids:
                errors.append(f"step {s.id!r} depends on unknown step {dep!r}")
        if s.budget_usd is not None and s.budget_usd <= 0:
            errors.append(f"step {s.id!r}: budget_usd must be > 0")
        if s.deadline_minutes is not None and s.deadline_minutes <= 0:
            errors.append(f"step {s.id!r}: deadline_minutes must be > 0")
        if s.max_retries < 1:
            errors.append(f"step {s.id!r}: max_retries must be >= 1")

    if spec.budget_usd is not None and spec.budget_usd <= 0:
        errors.append("graph budget_usd must be > 0")

    # Cycle detection via topological sort (Kahn's algorithm).
    if not errors or not any("unknown step" in e for e in errors):
        indegree = {s.id: len(s.depends_on) for s in spec.steps}
        ready = [s.id for s in spec.steps if indegree[s.id] == 0]
        order: list[str] = []
        deps_by = {s.id: [] for s in spec.steps}
        for s in spec.steps:
            for dep in s.depends_on:
                if dep in deps_by:
                    deps_by[dep].append(s.id)
        while ready:
            node = ready.pop()
            order.append(node)
            for child in deps_by[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if len(order) != len(spec.steps):
            cyclic = [i for i in ids if i not in order]
            errors.append(f"cycle detected among steps: {sorted(cyclic)}")

    return errors


def topological_waves(spec: GraphSpec) -> list[list[str]]:
    """Steps grouped into waves of parallelizable work, in dependency order.

    Pure and deterministic: wave 0 = no dependencies; wave k = steps whose
    dependencies are all in waves < k. Within a wave, steps run in
    parallel. Callers must validate first (behavior on cycles is
    unspecified but safe — it never hangs).
    """
    remaining = {s.id: set(s.depends_on) for s in spec.steps}
    waves: list[list[str]] = []
    done: set[str] = set()

    while remaining:
        wave = sorted(
            sid for sid, deps in remaining.items()
            if deps <= done
        )
        if not wave:  # cycle: break it by taking the lowest-id step
            wave = [sorted(remaining)[0]]
        waves.append(wave)
        for sid in wave:
            done.add(sid)
            remaining.pop(sid)
    return waves
