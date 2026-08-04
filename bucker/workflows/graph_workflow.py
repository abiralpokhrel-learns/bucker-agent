"""Graph workflow: run a multi-step task DAG durably (graph engineering).

Each wave of independent steps runs its child CodeTaskWorkflow IN
PARALLEL (asyncio.gather over child workflow handles — Temporal executes
them concurrently). Steps in a later wave wait for every dependency.

Purity rules (determinism discipline):
  * no I/O in workflow code — everything stateful happens in activities;
  * iteration order is deterministic (sorted step ids);
  * the whole graph is a single task in the event store, so the audit
    trail covers the run and replay works per child task.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from bucker.activities.graph import (
        record_graph_step,
        register_graph_step,
    )
    from bucker.contracts.graph import parse_spec, topological_waves, validate_graph
    from bucker.workflows.code_task_workflow import (
        CodeTaskInput,
        CodeTaskWorkflow,
    )


@dataclass(slots=True)
class GraphInput:
    graph_task_id: str
    spec: dict[str, Any]
    budget_usd: float | None = None
    fail_fast: bool = False


@workflow.defn
class GraphWorkflow:
    @workflow.run
    async def run(self, inp: GraphInput) -> dict[str, Any]:
        try:
            spec = parse_spec(inp.spec)
        except ValueError as exc:
            await workflow.execute_activity(
                record_graph_step,
                args=[inp.graph_task_id, "__graph__", "failed",
                      {"errors": [str(exc)]}],
                start_to_close_timeout=timedelta(seconds=30),
            )
            return {"graph_task_id": inp.graph_task_id, "status": "invalid",
                    "errors": [str(exc)]}

        errors = validate_graph(spec)
        if errors:
            await workflow.execute_activity(
                record_graph_step,
                args=[inp.graph_task_id, "__graph__", "failed",
                      {"errors": errors}],
                start_to_close_timeout=timedelta(seconds=30),
            )
            return {"graph_task_id": inp.graph_task_id, "status": "invalid",
                    "errors": errors}

        steps_by_id = {s.id: s for s in spec.steps}
        results: dict[str, dict[str, Any]] = {}
        failed: set[str] = set()

        await workflow.execute_activity(
            record_graph_step,
            args=[inp.graph_task_id, "__graph__", "started",
                  {"name": spec.name, "steps": len(spec.steps)}],
            start_to_close_timeout=timedelta(seconds=30),
        )

        for wave in topological_waves(spec):
            if spec.fail_fast and failed:
                break

            async def run_step(step_id: str) -> dict[str, Any]:
                step = steps_by_id[step_id]
                try:
                    task_id = await workflow.execute_activity(
                        register_graph_step,
                        args=[inp.graph_task_id, step_id,
                              step.objective, step.task_type,
                              step.budget_usd],
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                except Exception as exc:  # noqa: BLE001
                    return {"step_id": step_id, "status": "registration_failed",
                            "error": str(exc)[:200]}

                child = await workflow.execute_child_workflow(
                    CodeTaskWorkflow.run,
                    CodeTaskInput(
                        task_id=task_id,
                        objective=step.objective,
                        budget_usd=step.budget_usd,
                        deadline_minutes=step.deadline_minutes,
                        max_retries=step.max_retries,
                        adaptive=False,
                    ),
                    id=f"graph-{inp.graph_task_id}-step-{step_id}",
                )
                return {"step_id": step_id, "task_id": task_id, **child}

            # Deterministic iteration: parallel within the wave, ordered ids.
            wave_outcomes = await asyncio.gather(
                *[run_step(sid) for sid in wave]
            )
            for outcome in wave_outcomes:
                sid = outcome["step_id"]
                results[sid] = outcome
                verdict = outcome.get("status", "unknown")
                if verdict == "failed" or "failed" in str(verdict):
                    failed.add(sid)
                await workflow.execute_activity(
                    record_graph_step,
                    args=[inp.graph_task_id, sid, verdict, outcome],
                    start_to_close_timeout=timedelta(seconds=30),
                )

        await workflow.execute_activity(
            record_graph_step,
            args=[inp.graph_task_id, "__graph__", "completed",
                  {"steps": len(results), "failed": sorted(failed)}],
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {
            "graph_task_id": inp.graph_task_id,
            "status": "completed",
            "failed": sorted(failed),
            "steps": results,
        }
