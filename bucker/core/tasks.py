"""Shared task lifecycle: create, query, and list tasks (BUILD_PLAN step 24).

Extracted from the API layer so every entry point (HTTP API, CLI, MCP
server, scheduler) drives the SAME code path — one way to create a task,
one way to read it back. A re-run is literally the same path as a new task.

This module is I/O (activities layer): it talks to Postgres and Temporal.
The workflow itself stays pure; this is where the side effects live.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

from bucker.config import settings
from bucker.core.events import EventType
from bucker.core.eventstore import EventStore

#: In-flight lite-mode runners. Kept alive so the event loop never GCs a
#: running task; the done-callback removes each entry when it finishes.
_LITE_TASKS: set[asyncio.Task] = set()


def _in_lite_mode(pool: Any) -> bool:
    """True when the pool is a LitePool — the no-Docker/no-Temporal mode.

    The pool type IS the mode: ``create_pool`` returns a LitePool for
    ``sqlite:`` DSNs and an asyncpg pool otherwise, so checking the
    concrete type is the single source of truth (no env var to drift).
    Setting ``BUCKER_SANDBOX_MODE=local`` additionally turns off the
    Docker sandbox; Temporal is never touched because the in-process
    runner replaces it.
    """
    from bucker.lite.pool import LitePool

    return isinstance(pool, LitePool)


def _lite_task_for(task_id: str) -> asyncio.Task | None:
    """Find the in-process runner task for a task id, if it is still running.

    Lite mode spawns one asyncio task per task; the cancel endpoint uses
    this to terminate it. Returns None when the task is unknown or done.
    """
    for task in _LITE_TASKS:
        meta = getattr(task, "_bucker_meta", None)
        if meta and meta.get("task_id") == task_id and not task.done():
            return task
    return None


async def register_task(
    store: EventStore,
    pool: Any,
    *,
    objective: str,
    task_type: str = "code",
    verifier: str | None = None,
    budget_usd: float | None = None,
) -> str:
    """Create the task row + TaskCreated event. Returns the new task_id.

    Does NOT start any workflow — callers that need execution use
    create_task(); the scheduler uses this to mint a fresh task per run
    before handing it to a child workflow.
    """
    task_id = uuid4()

    # The planner chooses the verifier for the real pipeline; demo tasks
    # default to noop.
    demo_types = ("demo",)
    final_verifier = (verifier or "noop") if task_type in demo_types else None

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, task_type, objective, status, verifier, budget_usd) "
            "VALUES ($1, $2, $3, 'pending', $4, $5)",
            task_id, task_type, objective, final_verifier, budget_usd,
        )

    payload: dict[str, Any] = {
        "objective": objective,
        "task_type": task_type,
        "budget_usd": budget_usd,
    }
    if final_verifier:
        payload["verifier"] = final_verifier
    await store.append(
        task_id,
        "TaskCreated",
        payload,
        idempotency_key=f"{task_id}:created",
    )
    return str(task_id)


async def start_task_workflow(
    task_id: UUID,
    *,
    objective: str,
    task_type: str = "code",
    budget_usd: float | None = None,
    deadline_minutes: int | None = None,
    max_retries: int = 2,
    adaptive: bool = False,
    graph_spec: dict | None = None,
) -> str:
    """Start the Temporal workflow for an EXISTING task row.

    Shared by create_task (fresh tasks) and reconcile_pending (recovery of
    registered-but-never-scheduled tasks). Returns the workflow id.
    Raises on failure — callers decide how to surface it.
    """
    from temporalio.client import Client

    client = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )
    if task_type in ("demo",):
        from bucker.workflows.task_workflow import TaskWorkflow, TaskWorkflowInput

        handle = await client.start_workflow(
            TaskWorkflow.run,
            TaskWorkflowInput(
                task_id=str(task_id),
                objective=objective,
                task_type=task_type,
            ),
            id=f"task-{task_id}",
            task_queue=settings.task_queue,
        )
        return handle.id
    if task_type == "graph" and graph_spec:
        from bucker.workflows.graph_workflow import GraphInput, GraphWorkflow

        handle = await client.start_workflow(
            GraphWorkflow.run,
            GraphInput(graph_task_id=str(task_id), spec=graph_spec),
            id=f"task-{task_id}",
            task_queue=settings.task_queue,
        )
        return handle.id
    from bucker.workflows.code_task_workflow import CodeTaskInput, CodeTaskWorkflow

    handle = await client.start_workflow(
        CodeTaskWorkflow.run,
        CodeTaskInput(
            task_id=str(task_id),
            objective=objective,
            max_retries=max_retries,
            budget_usd=budget_usd,
            deadline_minutes=deadline_minutes,
            adaptive=adaptive,
        ),
        id=f"task-{task_id}",
        task_queue=settings.task_queue,
    )
    return handle.id


async def create_task(
    store: EventStore,
    pool: Any,
    *,
    objective: str,
    task_type: str = "code",
    verifier: str | None = None,
    budget_usd: float | None = None,
    deadline_minutes: int | None = None,
    max_retries: int = 2,
    adaptive: bool = False,
    graph_spec: dict | None = None,
) -> tuple[str, str | None, str | None]:
    """Insert the task row, append TaskCreated, start the workflow.

    Returns (task_id, workflow_id, schedule_error). workflow_id is None
    when Temporal is unavailable; the failure is NOT silent — a
    ScheduleFailed event is appended, the status flips to
    'schedule_failed', and reconcile_pending() re-schedules it when
    Temporal returns.

    task_type="graph" with graph_spec starts the multi-step DAG workflow
    (graph engineering) instead of a single-pipeline run.
    """
    task_id = await register_task(
        store, pool,
        objective=objective,
        task_type=task_type,
        verifier=verifier,
        budget_usd=budget_usd,
    )

    # Start the task. In lite mode (sqlite storage, no Temporal) the
    # pipeline runs in-process as an asyncio task; otherwise a Temporal
    # workflow is started. Either way the failure is recorded, never silent.
    workflow_id = None
    schedule_error: str | None = None
    try:
        if _in_lite_mode(pool):
            from bucker.lite.runner import run_task_lite

            # Fire-and-track: the runner appends its own events. We hand
            # back a synthetic workflow id so the response shape matches
            # the Temporal path (workflow_id is a string or None).
            task = asyncio.create_task(
                run_task_lite(
                    str(task_id),
                    objective,
                    task_type=task_type,
                    budget_usd=budget_usd,
                    deadline_minutes=deadline_minutes,
                    max_retries=max_retries,
                    adaptive=adaptive,
                    graph_spec=graph_spec,
                )
            )
            # Tag the task so the cancel endpoint can find it by task id.
            object.__setattr__(task, "_bucker_meta", {"task_id": str(task_id)})
            # Keep a reference so the event loop never GCs a running task.
            _LITE_TASKS.add(task)
            task.add_done_callback(_LITE_TASKS.discard)
            workflow_id = f"lite-{task_id}"
        else:
            workflow_id = await start_task_workflow(
                task_id,
                objective=objective,
                task_type=task_type,
                budget_usd=budget_usd,
                deadline_minutes=deadline_minutes,
                max_retries=max_retries,
                adaptive=adaptive,
                graph_spec=graph_spec,
            )
    except Exception as exc:  # noqa: BLE001 — a scheduling failure is visible, not silent
        # Hardening review: a swallowed scheduling error is a task black
        # hole. The task row EXISTS (registered) but no workflow is
        # running — persist the failure as an event + status so the
        # reconciler (and the operator) can see and fix it.
        schedule_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        await store.append(
            task_id,
            EventType.SCHEDULE_FAILED,
            {"error": schedule_error},
            idempotency_key=f"{task_id}:schedule-failed",
        )
        async with store._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tasks SET status = 'schedule_failed' WHERE id = $1",
                task_id,
            )

    return str(task_id), workflow_id, schedule_error


async def reconcile_pending(
    pool: Any,
    store: EventStore,
    *,
    dry_run: bool = False,
    max_age_minutes: int = 2,
) -> dict:
    """Re-schedule tasks whose workflow never started (hardening review).

    Finds tasks in 'pending'/'schedule_failed' status that are old enough
    to be genuine failures (not mid-registration), and attempts to start
    their workflows. Registered-but-never-scheduled is the black-hole
    failure mode; this is the reconciler that closes it. Existing task
    rows are reused — no new rows, no duplicated audit trails.

    Returns a report dict. With dry_run=True nothing is started.
    """
    from uuid import uuid4

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, task_type, objective, status, budget_usd
            FROM tasks
            WHERE status IN ('pending', 'schedule_failed')
              AND created_at < NOW() - ($1 || ' minutes')::interval
            ORDER BY created_at
            LIMIT 100
            """,
            str(max_age_minutes),
        )

    report = {"found": len(rows), "scheduled": 0, "failed": [], "skipped_dry_run": []}
    for row in rows:
        task_id: UUID = row["id"]
        if dry_run:
            report["skipped_dry_run"].append(str(task_id))
            continue
        try:
            await start_task_workflow(
                task_id,
                objective=row["objective"] or "",
                task_type=row["task_type"] or "code",
                budget_usd=row["budget_usd"],
            )
            report["scheduled"] += 1
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            report["failed"].append({"task_id": str(task_id), "error": error})
            # Re-record with a fresh key so the retry is visible in the
            # audit trail (the first failure's key is consumed).
            await store.append(
                task_id,
                EventType.SCHEDULE_FAILED,
                {"error": error, "reconciled": True},
                idempotency_key=f"{task_id}:schedule-failed:{uuid4().hex[:8]}",
            )
    return report


_TASK_ROW_SQL = """
SELECT t.id, t.task_type, t.objective, t.status, t.verifier,
       t.budget_usd, t.created_at,
       COALESCE(SUM(tm.cost_usd), 0) AS cost_usd,
       COALESCE(SUM(tm.total_tokens), 0) AS total_tokens,
       (SELECT COUNT(*) FROM events e WHERE e.task_id = t.id) AS event_count
FROM tasks t
LEFT JOIN telemetry tm ON tm.task_id = t.id
"""


async def get_task(pool: Any, task_id: UUID) -> dict | None:
    """One task with rolled-up cost/tokens/events, or None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _TASK_ROW_SQL + " WHERE t.id = $1 GROUP BY t.id",
            task_id,
        )
    if row is None:
        return None
    return _row_to_task(row)


async def list_tasks(pool: Any, limit: int = 50) -> list[dict]:
    """Recent tasks, newest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _TASK_ROW_SQL + " GROUP BY t.id ORDER BY t.created_at DESC LIMIT $1",
            limit,
        )
    return [_row_to_task(r) for r in rows]


# ---------------------------------------------------- human-in-the-loop ----


async def review_task(
    store: EventStore,
    task_id: UUID,
    *,
    approved: bool,
    note: str = "",
) -> dict:
    """Human review of an escalated (needs_human_review) task.

    The machine's verifier never passed, so the human is the judge. The
    verdict is recorded as an append-only event and the task status flips
    to the honest terminal values "human_approved" / "human_rejected" —
    deliberately distinct from the machine verdicts so the audit trail
    can never confuse the two.
    """
    task = await get_task(store._pool, task_id)
    if task is None:
        raise KeyError(f"task {task_id} not found")
    if task["status"] != "needs_human_review":
        raise ValueError(
            f"task status is {task['status']!r}; only needs_human_review "
            f"tasks can be approved or rejected"
        )

    status = "human_approved" if approved else "human_rejected"
    async with store._pool.acquire() as conn:
        await conn.execute(
            "UPDATE tasks SET status = $1 WHERE id = $2",
            status,
            task_id,
        )
    await store.append(
        task_id,
        EventType.HUMAN_APPROVED if approved else EventType.HUMAN_REJECTED,
        {"note": note.strip()[:500], "reviewer": "human"},
        idempotency_key=f"{task_id}:review-{status}",
    )
    return {"task_id": str(task_id), "status": status, "note": note.strip()[:500]}


def _row_to_task(row: Any) -> dict:
    return {
        "task_id": str(row["id"]),
        "task_type": row["task_type"],
        "objective": row["objective"],
        "status": row["status"],
        "verifier": row["verifier"],
        "budget_usd": row["budget_usd"],
        "cost_usd": float(row["cost_usd"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "event_count": int(row["event_count"] or 0),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }
