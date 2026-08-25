"""Lite-mode schedules: recurring verified tasks without Temporal.

Closes the biggest lite-mode gap (the API used to answer 501 here). The
schedule lives in the SAME SQLite database as everything else; a
background asyncio loop inside the dashboard/API process ticks every few
seconds, finds due schedules, and mints each run through ``create_task`` —
the identical code path a manual task uses, so every scheduled run gets
the full planner -> worker -> verifier pipeline, budget guard, and audit
trail.

What lite schedules give up vs. Temporal (honest, and by design):

* The scheduler only runs while the lite server process runs. If it is
  down at a fire time, that run is skipped — never replayed later.
  (Temporal would catch up after downtime.)
* Fire-time claiming advances ``next_run_at`` BEFORE spawning the task,
  so a crash mid-tick loses one run rather than double-firing. Missed,
  never duplicated, is the deliberate trade for exactly-once pipelines.
* One scheduler per database. Two lite servers on one SQLite file would
  both tick; use one.

Timezone and expression semantics come from bucker.core.cron (pure, fully
tested). Schedule CRUD may be called from any process (CLI included) —
the running server picks new schedules up within one tick.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from bucker.core.cron import CronError, next_fire, validate_cron

#: How often the loop looks for due schedules. A fire lands within this
#: window of its nominal time; 15s is far below any useful cron cadence.
TICK_SECONDS = 15.0


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------- store ----


async def create_schedule(pool: Any, spec: dict) -> dict:
    """Insert or update a schedule row. Re-creating an id UPDATES (the
    same re-runnable contract the Temporal path offers).

    spec keys: schedule_id, cron, template, objective, budget_usd,
    deadline_minutes, max_retries, adaptive, task_type.
    """
    from bucker.templates import resolve_template

    error = validate_cron(spec["cron"])
    if error:
        raise ValueError(f"invalid cron: {error}")

    template = resolve_template(spec["template"])
    objective = str(spec.get("objective") or template["objective"])

    first_run = next_fire(spec["cron"], _now())
    if first_run is None:
        raise ValueError("cron never fires within the search horizon")

    await pool.execute(
        """
        INSERT INTO schedules (
            id, cron, template, objective, task_type, budget_usd,
            deadline_minutes, max_retries, adaptive, paused, next_run_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 0, $10)
        ON CONFLICT (id) DO UPDATE SET
            cron = EXCLUDED.cron,
            template = EXCLUDED.template,
            objective = EXCLUDED.objective,
            task_type = EXCLUDED.task_type,
            budget_usd = EXCLUDED.budget_usd,
            deadline_minutes = EXCLUDED.deadline_minutes,
            max_retries = EXCLUDED.max_retries,
            adaptive = EXCLUDED.adaptive,
            paused = 0,
            next_run_at = EXCLUDED.next_run_at,
            updated_at = $11
        """,
        spec["schedule_id"],
        spec["cron"],
        spec["template"],
        objective,
        str(spec.get("task_type") or template.get("task_type") or "code_change"),
        spec.get("budget_usd"),
        spec.get("deadline_minutes"),
        int(spec.get("max_retries", 2)),
        1 if spec.get("adaptive") else 0,
        _iso(first_run),
        _iso(_now()),
    )
    return {
        "schedule_id": spec["schedule_id"],
        "cron": spec["cron"],
        "template": spec["template"],
        "objective": objective,
        "next_run_at": _iso(first_run),
        "created": True,
    }


_ROW_COLUMNS = (
    "id, cron, template, objective, task_type, budget_usd, "
    "deadline_minutes, max_retries, adaptive, paused, next_run_at, "
    "last_run_at, last_task_id, run_count, created_at"
)


def _row_to_dict(row: Any) -> dict:
    return {
        "schedule_id": row["id"],
        "cron": row["cron"],
        "template": row["template"],
        "objective": row["objective"] or "",
        "task_type": row["task_type"],
        "budget_usd": row["budget_usd"],
        "deadline_minutes": row["deadline_minutes"],
        "max_retries": row["max_retries"],
        "adaptive": bool(row["adaptive"]),
        "paused": bool(row["paused"]),
        "next_run_at": (
            row["next_run_at"].isoformat()
            if hasattr(row["next_run_at"], "isoformat")
            else row["next_run_at"]
        ) if row["next_run_at"] else None,
        "last_run_at": (
            row["last_run_at"].isoformat()
            if hasattr(row["last_run_at"], "isoformat")
            else row["last_run_at"]
        ) if row["last_run_at"] else None,
        "last_task_id": row["last_task_id"],
        "run_count": row["run_count"],
        "created_at": (
            row["created_at"].isoformat()
            if hasattr(row["created_at"], "isoformat")
            else row["created_at"]
        ),
    }


async def get_schedule(pool: Any, schedule_id: str) -> dict | None:
    row = await pool.fetchrow(
        f"SELECT {_ROW_COLUMNS} FROM schedules WHERE id = $1", schedule_id
    )
    return _row_to_dict(row) if row is not None else None


async def list_schedules(pool: Any) -> list[dict]:
    rows = await pool.fetch(
        f"SELECT {_ROW_COLUMNS} FROM schedules ORDER BY created_at DESC"
    )
    return [_row_to_dict(r) for r in rows]


async def delete_schedule(pool: Any, schedule_id: str) -> bool:
    tag = await pool.execute("DELETE FROM schedules WHERE id = $1", schedule_id)
    # execute returns asyncpg's command tag ("DELETE n"); LitePool mirrors it.
    try:
        deleted = int(tag.split()[-1])
    except (ValueError, AttributeError):
        deleted = 0
    return deleted > 0


async def set_paused(pool: Any, schedule_id: str, *, paused: bool) -> dict | None:
    """Pause or resume. Resuming recomputes next_run_at from NOW so a
    schedule paused across its fire time does not fire immediately."""
    row = await pool.fetchrow(
        "SELECT cron FROM schedules WHERE id = $1", schedule_id
    )
    if row is None:
        return None
    next_run: str | None = None
    if not paused:
        resume_at = next_fire(row["cron"], _now())
        next_run = _iso(resume_at) if resume_at else None
    await pool.execute(
        """
        UPDATE schedules SET paused = $2, next_run_at = $3, updated_at = $4
        WHERE id = $1
        """,
        schedule_id,
        1 if paused else 0,
        next_run,
        _iso(_now()),
    )
    return await get_schedule(pool, schedule_id)


# ------------------------------------------------------------- firing ----


async def due_schedules(pool: Any, now: datetime) -> list[dict]:
    """Schedules whose fire time has passed, oldest next-run first.

    The comparison parameter is produced by the same isoformat call site
    that writes next_run_at (see schema comment): SQLite string ordering is
    only correct between identically-formatted values.
    """
    rows = await pool.fetch(
        f"""
        SELECT {_ROW_COLUMNS} FROM schedules
        WHERE paused = 0 AND next_run_at IS NOT NULL AND next_run_at <= $1
        ORDER BY next_run_at ASC
        LIMIT 50
        """,
        _iso(now),
    )
    return [_row_to_dict(r) for r in rows]


async def mark_fired(
    pool: Any,
    schedule_id: str,
    *,
    fired_at: datetime,
    next_at: datetime | None,
    task_id: str | None,
) -> None:
    """Advance the schedule after a fire. Called BEFORE the task spawns so
    a crash between the two loses a run instead of duplicating one."""
    await pool.execute(
        """
        UPDATE schedules
        SET last_run_at = $2, last_task_id = $3, next_run_at = $4,
            run_count = run_count + 1, updated_at = $2
        WHERE id = $1
        """,
        schedule_id,
        _iso(fired_at),
        task_id,
        _iso(next_at) if next_at else None,
    )


class LiteScheduler:
    """The in-process loop that turns due schedule rows into real tasks."""

    def __init__(self, pool: Any, *, tick_seconds: float = TICK_SECONDS) -> None:
        self._pool = pool
        self._tick_seconds = tick_seconds
        self._loop_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(self._run(), name="lite-scheduler")
        print(f"  [lite] scheduler active (every {int(self._tick_seconds)}s)")

    async def stop(self) -> None:
        if self._loop_task is None:
            return
        self._loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._loop_task
        self._loop_task = None

    async def _run(self) -> None:
        while True:
            try:
                fired = await self.tick()
                if fired:
                    print(f"  [lite] scheduler fired {fired} scheduled run(s)")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the loop must survive
                print(f"  [lite] scheduler tick failed: "
                      f"{type(exc).__name__}: {str(exc)[:160]}")
            await asyncio.sleep(self._tick_seconds)

    async def tick(self) -> int:
        """Fire every due schedule once. Returns how many runs started."""
        from bucker.core.eventstore import EventStore
        from bucker.core.tasks import create_task

        now = _now()
        due = await due_schedules(self._pool, now)
        if not due:
            return 0

        store = EventStore(self._pool)
        fired = 0
        for schedule in due:
            fired_at = _now()
            following = next_fire(schedule["cron"], fired_at)

            # Claim FIRST (advance next_run_at), then mint the task. The
            # gap between the two writes is the crash window; losing a run
            # beats silently double-running a paid pipeline.
            await mark_fired(
                self._pool,
                schedule["schedule_id"],
                fired_at=fired_at,
                next_at=following,
                task_id=None,
            )

            task_id, workflow_id, schedule_error = await create_task(
                store,
                self._pool,
                objective=schedule["objective"],
                task_type=schedule["task_type"],
                budget_usd=schedule["budget_usd"],
                deadline_minutes=schedule["deadline_minutes"],
                max_retries=int(schedule["max_retries"] or 2),
                adaptive=bool(schedule["adaptive"]),
            )
            fired += 1
            # Link the minted task into the schedule row (best-effort: the
            # linkage is observability, not correctness).
            await self._pool.execute(
                "UPDATE schedules SET last_task_id = $2 WHERE id = $1",
                schedule["schedule_id"],
                task_id,
            )
            status = "scheduled" if workflow_id else f"NOT scheduled ({schedule_error})"
            print(f"  [lite] schedule {schedule['schedule_id']} -> task "
                  f"{task_id[:8]} ({status})")
        return fired


__all__ = [
    "TICK_SECONDS",
    "CronError",
    "LiteScheduler",
    "create_schedule",
    "delete_schedule",
    "due_schedules",
    "get_schedule",
    "list_schedules",
    "mark_fired",
    "set_paused",
]
