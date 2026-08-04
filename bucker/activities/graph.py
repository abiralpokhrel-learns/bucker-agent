"""Graph workflow activities (graph engineering).

* register_graph_step — mint a FRESH task row per graph step (same
  idempotency-safe pattern as the scheduler: each run of a step is its
  own task, its own audit trail, replayable independently).
* record_graph_step — append GraphStepCompleted events on the graph task.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity

from bucker.activities.demo import get_store
from bucker.core.events import EventType
from bucker.core.tasks import register_task


@activity.defn
async def register_graph_step(
    graph_task_id: str,
    step_id: str,
    objective: str,
    task_type: str,
    budget_usd: float | None,
) -> str:
    store = await get_store()
    return await register_task(
        store,
        store._pool,
        objective=objective,
        task_type=task_type,
        budget_usd=budget_usd,
    )


@activity.defn
async def record_graph_step(
    graph_task_id: str,
    step_id: str,
    status: str,
    detail: dict[str, Any] | None = None,
) -> None:
    from uuid import UUID

    store = await get_store()
    await store.append(
        UUID(graph_task_id),
        EventType.GRAPH_STEP_COMPLETED,
        {
            "step_id": step_id,
            "status": status,
            **({} if detail is None else {"detail": detail}),
        },
        idempotency_key=f"{graph_task_id}:graph-{step_id}-{status}",
    )
