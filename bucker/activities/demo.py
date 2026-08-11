"""Demo activities for the Phase 0 durability proof (steps 10-11).

[HAND] — the idempotency pattern here is the template every real activity
(planner, worker, verifier) will copy. Get it right once.

Why activities and not workflow code: workflow code must be deterministic and
replayable by Temporal, so it may not touch the network, the clock, or the DB.
All side effects live here.

The exactly-once pattern:

    key = f"{task_id}:{step}"          # stable across retries
    do_side_effect(idempotent_on=key)  # safe to repeat
    await store.append(..., idempotency_key=key)

If the process dies after the side effect but before the append, Temporal
retries the activity; the side effect is repeated harmlessly and the append
dedupes on the unique index. Exactly one event, one logical effect.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from temporalio import activity

from bucker.config import settings
from bucker.core.blob import BlobStore
from bucker.core.events import EventType
from bucker.core.eventstore import EventStore, create_pool

# --------------------------------------------------------------- runtime ----
# One pool per worker process, created lazily. Activities are plain async
# functions, so we cannot inject dependencies through the workflow.
_pool = None
_store: EventStore | None = None
_blobs: BlobStore | None = None
_lock = asyncio.Lock()


async def get_store() -> EventStore:
    global _pool, _store
    if _store is None:
        async with _lock:
            if _store is None:
                _pool = await create_pool(settings.database_url)
                _store = EventStore(_pool)
    return _store


def get_blobs() -> BlobStore:
    global _blobs
    if _blobs is None:
        _blobs = BlobStore(settings.blob_root)
    return _blobs


# ----------------------------------------------------------------- types ----
@dataclass
class StepInput:
    """Everything an activity needs. Dataclasses serialize cleanly for Temporal."""

    task_id: str
    step: str
    step_index: int
    # Set by the crash test: the step at which the worker should die.
    crash_at: str | None = None


# ------------------------------------------------------------ activities ----
@activity.defn
async def record_task_started(task_id: str, objective: str, task_type: str) -> int:
    store = await get_store()
    event = await store.append(
        UUID(task_id),
        EventType.TASK_STARTED,
        {"objective": objective, "task_type": task_type},
        idempotency_key=f"{task_id}:started",
    )
    return event.id


def should_inject_crash(
    workspace: Path, step: str, crash_at: str | None
) -> bool:
    """Decide whether to hard-kill the process at this step. One-shot.

    Extracted from ``run_step`` so it can be unit-tested — the caller does the
    actual ``os._exit``, which is untestable in-process.

    Why the on-disk marker: ``crash_at`` travels inside the workflow input, and
    Temporal hands a retried activity the *identical* input. A naive
    ``if crash_at == step: exit()`` therefore kills every replacement worker
    too, forever, and the task never progresses — the crash test just sits
    there until it times out. The marker file outlives the process (an
    in-memory flag cannot, since the process is what dies), so the retry sees
    "already crashed here once" and continues normally.

    Returns True at most once per (workspace, step).
    """
    if not crash_at or crash_at != step:
        return False

    crash_marker = workspace / f"{step}.crashed"
    if crash_marker.exists():
        return False

    crash_marker.write_text("crash injected once\n")
    return True


@activity.defn
async def run_step(inp: StepInput) -> int:
    """Perform one fake unit of work, durably.

    The 'side effect' is appending a line to a workspace file — deliberately
    something observable outside the DB, so the crash test can prove there is
    no duplication in the real world, not merely in the event log.
    """
    store = await get_store()
    task_id = UUID(inp.task_id)
    key = f"{inp.task_id}:{inp.step}"

    await store.append(
        task_id,
        EventType.STEP_STARTED,
        {"step": inp.step, "index": inp.step_index},
        idempotency_key=f"{key}:started",
    )

    # --- the side effect, made idempotent by keying on the step -----------
    workspace = Path(settings.blob_root).parent / "workspace" / inp.task_id
    workspace.mkdir(parents=True, exist_ok=True)
    marker = workspace / f"{inp.step}.done"
    if not marker.exists():                       # <- the idempotency guard
        await asyncio.sleep(0.2)                  # pretend work
        marker.write_text(f"{inp.step} completed\n")

    # --- crash injection for the Phase 0 proof (step 12) ------------------
    if should_inject_crash(workspace, inp.step, inp.crash_at):
        activity.logger.warning("CRASH INJECTION at step %s", inp.step)
        os._exit(137)

    output_ref = get_blobs().put_json(
        {"step": inp.step, "index": inp.step_index, "result": "ok"}
    )
    event = await store.append(
        task_id,
        EventType.STEP_COMPLETED,
        {"step": inp.step, "index": inp.step_index},
        tool_output_ref=output_ref,
        idempotency_key=f"{key}:completed",
    )
    return event.id


@activity.defn
async def record_task_completed(task_id: str) -> int:
    store = await get_store()
    event = await store.append(
        UUID(task_id),
        EventType.TASK_COMPLETED,
        {},
        idempotency_key=f"{task_id}:completed",
    )
    # The event is the source of truth; tasks.status is a denormalized
    # cache for listing/queries. Keep the cache honest at terminal
    # events (same pattern as record_decision in pipeline.py) — without
    # this a completed demo task shows "pending" in /tasks forever.
    try:
        async with store._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET status = 'completed' WHERE id = $1",
                UUID(task_id),
            )
    except Exception:  # noqa: BLE001 — the event already recorded; cache is best-effort
        pass
    return event.id
