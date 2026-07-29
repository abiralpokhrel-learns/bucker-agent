"""Planner activity — wraps the pure planner with event writes and telemetry.

The split matters: ``bucker.planner`` holds testable logic with no I/O; this
module is the only part that touches the event log, so the planner can be
tested against a fake router with no database in sight.
"""

from __future__ import annotations

from uuid import UUID

from temporalio import activity

from bucker.activities.demo import get_blobs, get_store
from bucker.core.events import EventType
from bucker.planner import PlanningFailed, generate_task_contract
from bucker.router.client import ModelRouter


@activity.defn
async def plan_task(task_id: str, objective: str) -> dict:
    """Generate a validated Task contract and record everything that happened.

    Every attempt lands in the event log — the failures especially. A rising
    ``SchemaValidationFailed`` rate is an early warning that a prompt or a model
    has regressed, and it is only visible if the failures were never discarded.
    """
    store = await get_store()
    tid = UUID(task_id)
    router = ModelRouter(get_blobs())

    await store.append(
        tid,
        EventType.PLAN_REQUESTED,
        {"objective": objective, "model": router.model, "mode": router.mode},
        idempotency_key=f"{task_id}:plan-requested",
    )

    try:
        result = await generate_task_contract(router, objective)
    except PlanningFailed as exc:
        for i, attempt in enumerate(exc.attempts):
            await store.append(
                tid,
                EventType.SCHEMA_VALIDATION_FAILED,
                {"attempt": i + 1, "errors": attempt.errors},
                tool_output_ref=attempt.response.raw_ref,
                idempotency_key=f"{task_id}:plan-invalid-{i + 1}",
            )
        await store.append(
            tid,
            EventType.TASK_FAILED,
            {"reason": "planner produced no valid contract", "attempts": len(exc.attempts)},
            idempotency_key=f"{task_id}:plan-failed",
        )
        raise

    # Record the failed attempts that preceded a successful repair.
    for i, attempt in enumerate(result.attempts[:-1]):
        await store.append(
            tid,
            EventType.SCHEMA_VALIDATION_FAILED,
            {"attempt": i + 1, "errors": attempt.errors, "repaired": True},
            tool_output_ref=attempt.response.raw_ref,
            idempotency_key=f"{task_id}:plan-invalid-{i + 1}",
        )

    final = result.attempts[-1]
    for i, attempt in enumerate(result.attempts):
        await store.append(
            tid,
            EventType.MODEL_CALL_COMPLETED,
            {
                "purpose": "planner",
                "model": attempt.response.model,
                "cost_usd": attempt.response.cost_usd,
                "latency_ms": attempt.response.latency_ms,
                "from_recording": attempt.response.from_recording,
            },
            tool_output_ref=attempt.response.raw_ref,
            idempotency_key=f"{task_id}:plan-call-{i + 1}",
        )

    await store.append(
        tid,
        EventType.PLAN_GENERATED,
        {
            "plan": result.task.model_dump(),
            "attempts": len(result.attempts),
            "repaired": result.repaired,
            "cost_usd": result.cost_usd,
        },
        tool_output_ref=final.response.raw_ref,
        idempotency_key=f"{task_id}:plan-generated",
    )

    return result.task.model_dump()
