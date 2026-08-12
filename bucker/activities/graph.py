"""Graph workflow activities (graph engineering).

* register_graph_step — mint a FRESH task row per graph step (same
  idempotency-safe pattern as the scheduler: each run of a step is its
  own task, its own audit trail, replayable independently).
* record_graph_step — append GraphStepCompleted events on the graph task.
"""

from __future__ import annotations

from typing import Any

from bucker.activities.demo import get_store
from bucker.core.events import EventType
from bucker.core.tasks import register_task
from bucker.temporal_compat import activity


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

    # The __graph__ bookend is the graph task's terminal event. The event
    # is the source of truth; tasks.status is a denormalized cache for
    # listing/queries. Keep the cache honest here (same pattern as
    # record_task_completed / record_decision) — without this, a finished
    # graph stays "pending" in the /tasks list forever even though the
    # folded state (events) says otherwise.
    if step_id == "__graph__" and status in ("completed", "failed"):
        failed = (detail or {}).get("failed") or []
        row_status = "failed" if (status == "failed" or failed) else "completed"
        try:
            async with store._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE tasks SET status = $2 WHERE id = $1",
                    UUID(graph_task_id),
                    row_status,
                )
        except Exception:  # noqa: BLE001 — event already recorded; cache is best-effort
            pass
