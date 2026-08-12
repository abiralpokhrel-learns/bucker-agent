"""Scheduled-task activity: mint a fresh task row for each schedule tick."""

from __future__ import annotations

from bucker.activities.demo import get_store
from bucker.core.tasks import register_task
from bucker.temporal_compat import activity


@activity.defn
async def register_scheduled_task(
    objective: str,
    task_type: str,
    budget_usd: float | None,
) -> str:
    """Register a NEW task for one scheduled run.

    A fresh task_id per tick is the whole point: the audit trail stays
    append-only and per-run, and idempotency keys never collide across
    runs of the same schedule.
    """
    store = await get_store()
    return await register_task(
        store,
        store._pool,  # noqa: SLF001 — same pattern as pipeline.py
        objective=objective,
        task_type=task_type,
        budget_usd=budget_usd,
    )
