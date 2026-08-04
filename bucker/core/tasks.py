"""Shared task lifecycle: create, query, and list tasks (BUILD_PLAN step 24).

Extracted from the API layer so every entry point (HTTP API, CLI, MCP
server, scheduler) drives the SAME code path — one way to create a task,
one way to read it back. A re-run is literally the same path as a new task.

This module is I/O (activities layer): it talks to Postgres and Temporal.
The workflow itself stays pure; this is where the side effects live.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from bucker.config import settings
from bucker.core.eventstore import EventStore


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
) -> tuple[str, str | None]:
    """Insert the task row, append TaskCreated, start the workflow.

    Returns (task_id, workflow_id). workflow_id is None when Temporal is
    unavailable — the task still exists, pending, and a worker picks it up
    when Temporal returns.

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

    # Start a Temporal workflow if Temporal is available; otherwise the
    # task sits pending and a worker picks it up later.
    workflow_id = None
    demo_types = ("demo",)
    try:
        from temporalio.client import Client

        client = await Client.connect(
            settings.temporal_host, namespace=settings.temporal_namespace
        )
        if task_type in demo_types:
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
        else:
            from bucker.workflows.code_task_workflow import (
                CodeTaskInput,
                CodeTaskWorkflow,
            )

            if task_type == "graph" and graph_spec:
                from bucker.workflows.graph_workflow import (
                    GraphInput,
                    GraphWorkflow,
                )

                handle = await client.start_workflow(
                    GraphWorkflow.run,
                    GraphInput(
                        graph_task_id=str(task_id),
                        spec=graph_spec,
                    ),
                    id=f"task-{task_id}",
                    task_queue=settings.task_queue,
                )
            else:
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
        workflow_id = handle.id
    except Exception:
        workflow_id = None

    return str(task_id), workflow_id


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
