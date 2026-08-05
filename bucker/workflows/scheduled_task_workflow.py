"""Scheduled-task runner workflow.

A Temporal Schedule fires this workflow on its cron. Each run:

  1. registers a FRESH task (new task_id, new TaskCreated event — the audit
     trail stays append-only and per-run; a schedule never reuses a task id
     or its idempotency keys),
  2. runs the REAL pipeline as a child CodeTaskWorkflow on that task.

The child reuses the exact plan -> work -> verify -> retry loop; this
workflow is only the per-tick envelope. Because Temporal starts a new
execution per tick (same workflow id, previous one completed), a missed
tick is retried exactly once and the schedule is durable across restarts.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from bucker.activities.notify import notify_task_result
    from bucker.activities.schedule import register_scheduled_task
    from bucker.workflows.code_task_workflow import CodeTaskInput, CodeTaskWorkflow


@dataclass
class ScheduledTaskInput:
    """What a scheduled run should do (fixed by the schedule)."""

    objective: str
    task_type: str = "code_change"
    budget_usd: float | None = None
    deadline_minutes: int | None = None
    max_retries: int = 2
    adaptive: bool = False


@workflow.defn
class ScheduledTaskWorkflow:
    def _opts(self, minutes: int = 5) -> dict:
        return {
            "start_to_close_timeout": timedelta(minutes=minutes),
            "retry_policy": RetryPolicy(
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30),
                maximum_attempts=3,
            ),
        }

    @workflow.run
    async def run(self, inp: ScheduledTaskInput) -> dict:
        task_id = await workflow.execute_activity(
            register_scheduled_task,
            args=[inp.objective, inp.task_type, inp.budget_usd],
            **self._opts(2),
        )

        result = await workflow.execute_child_workflow(
            CodeTaskWorkflow.run,
            CodeTaskInput(
                task_id=task_id,
                objective=inp.objective,
                max_retries=inp.max_retries,
                budget_usd=inp.budget_usd,
                deadline_minutes=inp.deadline_minutes,
                adaptive=inp.adaptive,
            ),
            id=f"task-{task_id}",
            # No task_queue: the child inherits the parent's queue, so the
            # schedule uses whatever queue the worker is registered on.
        )
        # Gateway (iter 7): announce the run's outcome where the user is.
        with contextlib.suppress(Exception):
            await workflow.execute_activity(
                notify_task_result,
                args=["task", {**result, "task_id": task_id}],
                start_to_close_timeout=timedelta(seconds=30),
            )
        return {**result, "task_id": task_id, "scheduled": True}
