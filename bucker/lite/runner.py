"""Lite-mode task runner: execute the pipeline in-process, no Temporal.

Lite mode replaces Temporal's orchestration with a plain asyncio loop
that calls the SAME activities (plan_task, run_worker, run_verifier,
evaluate_policy, record_decision, record_failure) directly. The
activities are plain async functions — ``@activity.defn`` only registers
them for the worker; calling them directly is fully supported.

What this gives up vs. Temporal (honest):

* No worker-crash recovery mid-task. If this process dies, the task is
  stuck in its last recorded state. Lite mode runs tasks to completion
  in one process or not at all.
* No durable retry of activities. The pipeline's own retry loop
  (verification -> evaluate_policy -> RETRY) is intact and identical;
  only Temporal's *activity-level* retries are gone.
* No workflow replay, no phase queries from outside.

The orchestration logic mirrors ``code_task_workflow.run`` and
``task_workflow.run`` (demo), including the budget pre-spend guard, so a
task behaves the same way in lite mode as it does on the full stack.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from bucker.activities.demo import (
    StepInput,
    record_task_completed,
    record_task_started,
    run_step,
)
from bucker.activities.graph import record_graph_step, register_graph_step
from bucker.activities.pipeline import (
    choose_adaptive_strategy,
    consolidate_task_memory,
    evaluate_policy,
    record_decision,
    record_failure,
    run_verifier,
    run_worker,
)
from bucker.activities.planner import plan_task
from bucker.retry import Action
from bucker.workflows.task_workflow import DEMO_STEPS


def _now_utc() -> datetime:
    return datetime.now(UTC)


async def run_demo_task(task_id: str, objective: str, task_type: str = "demo") -> dict:
    """Mirror task_workflow.run: record start, 5 demo steps, record done."""
    await record_task_started(task_id, objective, task_type)
    for index, step in enumerate(DEMO_STEPS):
        await run_step(StepInput(task_id=task_id, step=step, step_index=index))
    await record_task_completed(task_id)
    return {"task_id": task_id, "steps": list(DEMO_STEPS), "status": "completed"}


async def run_code_task(
    task_id: str,
    objective: str,
    *,
    budget_usd: float | None = None,
    deadline_minutes: int | None = None,
    max_retries: int = 2,
    adaptive: bool = False,
) -> dict:
    """Mirror code_task_workflow.run: plan -> work -> verify -> decide.

    Returns a dict with the same terminal shape the workflow returns:
    ``{"status": "completed"|"failed"|"needs_human_review"|"halted", ...}``.
    """
    started = _now_utc()
    total_cost = 0.0
    cost_unknown = False
    current_model: str | None = None
    passed: list[bool] = []
    diagnostics: list[str] = []
    models_used: list[str] = []
    last_verdict: dict = {}

    # --- plan ------------------------------------------------------------
    try:
        task_dict, plan_cost, plan_unknown = await plan_task(task_id, objective)
    except Exception as exc:  # noqa: BLE001 — any planner failure is terminal
        await _safe_record_failure(task_id, f"planning failed: {exc}", 0)
        return {"status": "failed", "attempts": 0, "reason": f"planning failed: {exc}"}
    total_cost += float(plan_cost or 0.0)
    cost_unknown = cost_unknown or bool(plan_unknown)

    budget = budget_usd or task_dict.get("budget_usd")
    deadline = deadline_minutes or task_dict.get("deadline_minutes")

    # --- work / verify / decide -----------------------------------------
    for attempt in range(1, max_retries + 2):
        # Halt BEFORE the next model spend (same guard as the workflow).
        pre = _pre_spend(
            total_cost, budget, started, deadline, attempt, cost_unknown
        )
        if pre is not None:
            await _safe_decision(task_id, pre, attempt)
            return {
                "status": "halted",
                "attempts": attempt,
                "reason": pre["reason"],
            }

        try:
            result_dict, worker_cost, worker_unknown = await run_worker(
                task_id, task_dict, attempt, current_model
            )
        except Exception as exc:  # noqa: BLE001
            await _safe_record_failure(
                task_id, f"worker activity failed: {exc}", attempt
            )
            return {"status": "failed", "attempts": attempt, "reason": str(exc)}
        total_cost += float(worker_cost or 0.0)
        cost_unknown = cost_unknown or bool(worker_unknown)

        try:
            last_verdict = await run_verifier(task_id, task_dict, result_dict, attempt)
        except Exception as exc:  # noqa: BLE001
            await _safe_record_failure(
                task_id, f"verifier activity failed: {exc}", attempt
            )
            return {"status": "failed", "attempts": attempt, "reason": str(exc)}

        passed.append(bool(last_verdict.get("passed")))
        diagnostics.append(str(last_verdict.get("diagnostics", "")))
        models_used.append(current_model or "")

        elapsed_minutes = (_now_utc() - started).total_seconds() / 60.0
        decision = await evaluate_policy(
            {
                "attempt": attempt,
                "max_retries": max_retries,
                "verification_passed": last_verdict["passed"],
                "diagnostics": last_verdict.get("diagnostics", ""),
                "cost_usd": total_cost,
                "budget_usd": budget,
                "elapsed_minutes": elapsed_minutes,
                "deadline_minutes": deadline,
            }
        )
        await _safe_decision(task_id, decision, attempt)

        action = Action(decision["action"])
        if action is Action.COMPLETE:
            await _safe_remember(task_id)
            return {
                "status": "completed",
                "attempts": attempt,
                "verdict": last_verdict,
            }
        if action is Action.ESCALATE:
            await _safe_remember(task_id)
            return {
                "status": "needs_human_review",
                "attempts": attempt,
                "reason": decision["reason"],
                "verdict": last_verdict,
            }
        if action is Action.HALT:
            return {
                "status": "halted",
                "attempts": attempt,
                "reason": decision["reason"],
            }

        # RETRY — feed the failure forward, same as the workflow.
        if adaptive:
            # choose_adaptive_strategy is itself a model call — the budget
            # guard applies to it too (mirrors the workflow's check before
            # the strategy call for the NEXT attempt).
            pre = _pre_spend(
                total_cost, budget, started, deadline, attempt + 1, cost_unknown
            )
            if pre is not None:
                await _safe_decision(task_id, pre, attempt)
                return {
                    "status": "halted",
                    "attempts": attempt,
                    "reason": pre["reason"],
                }
            with contextlib.suppress(Exception):
                strategy = await choose_adaptive_strategy(
                    {
                        "attempt": attempt,
                        "verifier_name": last_verdict.get("verifier", ""),
                        "objective": task_dict["objective"],
                        "failure_context": decision["failure_context"],
                        "diagnostics": list(diagnostics),
                        "passed": list(passed),
                        "models_used": list(models_used),
                        "current_model": current_model,
                    }
                )
                if strategy.get("next_objective"):
                    task_dict = {**task_dict, "objective": strategy["next_objective"]}
                if strategy.get("next_model"):
                    current_model = strategy["next_model"]
        else:
            task_dict = {
                **task_dict,
                "objective": (
                    f"{task_dict['objective']}\n\n{decision['failure_context']}"
                ),
            }

    return {
        "status": "needs_human_review",
        "attempts": max_retries + 1,
        "reason": "retry loop exhausted without a terminal decision",
        "verdict": last_verdict,
    }


def _pre_spend(
    cost_usd: float,
    budget: float | None,
    started: datetime,
    deadline: int | None,
    attempt: int,
    cost_unknown: bool,
) -> dict | None:
    """Same pre-spend halt guard as the workflow's ``_pre_spend_decision``.

    ``next_step_estimate`` matches the workflow's default step estimate
    (CodeTaskInput.step_estimate_usd = 0.02) so lite mode enforces the
    same budget ceiling as the full stack.
    """
    from bucker.core.budget import pre_spend_decision

    elapsed_minutes = (_now_utc() - started).total_seconds() / 60.0
    decision = pre_spend_decision(
        cost_usd, budget, elapsed_minutes, deadline, attempt,
        next_step_estimate=0.02,  # same default as CodeTaskInput
        cost_unknown=cost_unknown,
    )
    return decision


async def _safe_decision(task_id: str, decision: dict, attempt: int) -> None:
    """Record a policy decision; never let the recording fail the task."""
    with contextlib.suppress(Exception):
        await record_decision(task_id, decision, attempt)


async def _safe_record_failure(task_id: str, reason: str, attempt: int) -> None:
    """Record a terminal failure event; best-effort like the workflow."""
    with contextlib.suppress(Exception):
        await record_failure(task_id, reason, attempt)


async def _safe_remember(task_id: str) -> None:
    """Episodic -> semantic memory distillation; never fails the task.

    Mirrors the workflow's ``_remember``: fire-and-forget with a short
    timeout, because a memory failure must not fail the task it is
    remembering.
    """
    with contextlib.suppress(Exception):
        await asyncio.wait_for(consolidate_task_memory(task_id), timeout=60)


async def run_graph_task(
    graph_task_id: str,
    spec: dict,
    *,
    max_retries: int = 2,
) -> dict:
    """Mirror graph_workflow.run: topological waves of in-process tasks.

    Each graph step becomes a task row + an in-process code run (the same
    path ``run_code_task`` uses), recorded via the graph activities so the
    event stream and dashboard look identical to the Temporal path.
    """
    from bucker.contracts.graph import parse_spec, topological_waves, validate_graph

    try:
        parsed = parse_spec(spec)
    except ValueError as exc:
        await _safe_graph_step(graph_task_id, "__graph__", "failed",
                               {"errors": [str(exc)]})
        return {"graph_task_id": graph_task_id, "status": "invalid",
                "errors": [str(exc)]}

    errors = validate_graph(parsed)
    if errors:
        await _safe_graph_step(graph_task_id, "__graph__", "failed",
                               {"errors": errors})
        return {"graph_task_id": graph_task_id, "status": "invalid",
                "errors": errors}

    steps_by_id = {s.id: s for s in parsed.steps}
    results: dict[str, dict] = {}
    failed: set[str] = set()

    await _safe_graph_step(graph_task_id, "__graph__", "started",
                           {"name": parsed.name, "steps": len(parsed.steps)})

    for wave in topological_waves(parsed):
        if parsed.fail_fast and failed:
            break

        async def run_step(step_id: str) -> dict:
            step = steps_by_id[step_id]
            try:
                task_id = await register_graph_step(
                    graph_task_id, step_id, step.objective,
                    step.task_type, step.budget_usd,
                )
            except Exception as exc:  # noqa: BLE001
                return {"step_id": step_id, "status": "registration_failed",
                        "error": str(exc)[:200]}

            outcome = await run_task_lite(
                task_id,
                step.objective,
                task_type=step.task_type,
                budget_usd=step.budget_usd,
                deadline_minutes=step.deadline_minutes,
                max_retries=step.max_retries or max_retries,
                adaptive=False,
            )
            return {"step_id": step_id, "task_id": task_id, **outcome}

        wave_outcomes = await asyncio.gather(*[run_step(sid) for sid in wave])
        for outcome in wave_outcomes:
            sid = outcome["step_id"]
            results[sid] = outcome
            verdict = outcome.get("status", "unknown")
            if verdict == "failed" or "failed" in str(verdict):
                failed.add(sid)
            await _safe_graph_step(graph_task_id, sid, verdict, outcome)

    await _safe_graph_step(graph_task_id, "__graph__", "completed",
                           {"steps": len(results), "failed": sorted(failed)})
    return {
        "graph_task_id": graph_task_id,
        "status": "completed",
        "failed": sorted(failed),
        "steps": results,
    }


async def _safe_graph_step(graph_task_id: str, step_id: str, status: str,
                           payload: dict) -> None:
    """Record one graph step event; never let the recording fail the graph."""
    import contextlib

    with contextlib.suppress(Exception):
        await record_graph_step(graph_task_id, step_id, status, payload)


async def run_task_lite(
    task_id: str,
    objective: str,
    *,
    task_type: str = "code_change",
    budget_usd: float | None = None,
    deadline_minutes: int | None = None,
    max_retries: int = 2,
    adaptive: bool = False,
    graph_spec: dict | None = None,
) -> dict[str, Any]:
    """Dispatch to the right in-process runner by task type."""
    if task_type == "demo":
        return await run_demo_task(task_id, objective, task_type)
    if task_type == "graph" and graph_spec:
        return await run_graph_task(task_id, graph_spec, max_retries=max_retries)
    return await run_code_task(
        task_id,
        objective,
        budget_usd=budget_usd,
        deadline_minutes=deadline_minutes,
        max_retries=max_retries,
        adaptive=adaptive,
    )
