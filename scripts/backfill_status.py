"""Backfill tasks.status from the event stream (the folded-pending fix).

tasks.status is a denormalized cache; the event stream is the source of
truth. Rows created before record_decision kept the cache honest are
stuck at 'pending' even when the task is terminal. This script replays
each task's events and syncs the cache.

Safe to re-run (idempotent). Only terminal events update the cache.

Usage: uv run python -m scripts.backfill_status
"""

from __future__ import annotations

import asyncio

from bucker.config import settings
from bucker.core.eventstore import EventStore, create_pool

TERMINAL_EVENTS = {
    "TaskCompleted": "completed",
    "NeedsHumanReview": "needs_human_review",
    "BudgetExceeded": "halted",
    "DeadlineExceeded": "halted",
    "HumanApproved": "human_approved",
    "HumanRejected": "human_rejected",
}


async def main() -> None:
    pool = await create_pool(settings.database_url)
    store = EventStore(pool)
    async with pool.acquire() as conn:
        ids = await conn.fetch("SELECT id FROM tasks")

    updated = 0
    for row in ids:
        task_id = row["id"]
        events = await store.read_stream(task_id)
        status = None
        for e in events:
            if e.event_type in TERMINAL_EVENTS:
                status = TERMINAL_EVENTS[e.event_type]
        if status is None:
            continue
        async with pool.acquire() as conn:
            current = await conn.fetchval(
                "SELECT status FROM tasks WHERE id = $1", task_id
            )
            if current != status:
                await conn.execute(
                    "UPDATE tasks SET status = $1 WHERE id = $2", status, task_id
                )
                updated += 1

    print(f"backfill done: {updated} task(s) synced")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
