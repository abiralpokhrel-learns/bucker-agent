"""Scheduled tasks (recurring verified execution).

A schedule runs the SAME verified pipeline as a manual task, on a cron
expression — "verify the staging deploy every morning", "re-run this
benchmark weekly". Every run is a normal task: durable, audited,
budgeted. The schedule itself lives in Temporal (the durable source of
truth); this module is a thin, typed wrapper around the client API.

A schedule is created by TEMPLATE: instead of an objective, you name a
task template (see bucker/templates.py) whose objective and defaults the
schedule fills in. That keeps schedules declarative and reviewable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """What a schedule runs and how often."""

    schedule_id: str
    cron: str                      # standard 5-field cron, e.g. "0 9 * * 1-5"
    template: str                  # task template name (bucker/templates.py)
    objective: str = ""            # override the template's objective
    task_type: str = "code_change"
    budget_usd: float | None = None
    deadline_minutes: int | None = None
    max_retries: int = 2
    adaptive: bool = False

    def as_dict(self) -> dict:
        return {
            "schedule_id": self.schedule_id,
            "cron": self.cron,
            "template": self.template,
            "objective": self.objective,
            "task_type": self.task_type,
            "budget_usd": self.budget_usd,
            "deadline_minutes": self.deadline_minutes,
            "max_retries": self.max_retries,
            "adaptive": self.adaptive,
        }


async def _client():
    from temporalio.client import Client

    from bucker.config import settings

    return await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )


async def create_schedule(spec: ScheduleSpec) -> dict:
    """Create (or update) a Temporal schedule for the spec.

    Creating with the same schedule_id twice UPDATES instead of erroring —
    idempotent by construction, so the CLI/API can be re-run safely.

    The schedule action starts ScheduledTaskWorkflow, which mints a FRESH
    task per tick and runs the real pipeline as a child workflow — a
    schedule never reuses a task id, so the audit trail stays per-run.
    """
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleState,
    )
    from temporalio.client import (
        ScheduleSpec as TSpec,
    )

    from bucker.config import settings
    from bucker.templates import resolve_template
    from bucker.workflows.scheduled_task_workflow import (
        ScheduledTaskInput,
        ScheduledTaskWorkflow,
    )

    template = resolve_template(spec.template)
    objective = spec.objective or template["objective"]

    action = ScheduleActionStartWorkflow(
        workflow=ScheduledTaskWorkflow.run,
        args=[ScheduledTaskInput(
            objective=objective,
            task_type=spec.task_type,
            budget_usd=spec.budget_usd,
            deadline_minutes=spec.deadline_minutes,
            max_retries=spec.max_retries,
            adaptive=spec.adaptive,
        )],
        id=f"scheduled-{spec.schedule_id}",
        task_queue=settings.task_queue,
    )
    client = await _client()
    schedule = Schedule(
        action=action,
        spec=TSpec(cron_expressions=[spec.cron]),
        state=ScheduleState(paused=False),
    )
    try:
        await client.create_schedule(spec.schedule_id, schedule)
    except Exception as exc:  # noqa: BLE001
        # Same id again = UPDATE, not an error: the CLI/API are re-runnable.
        # temporalio's handle.update takes a callable from input to update.
        if type(exc).__name__ == "ScheduleAlreadyRunningError":
            from temporalio.client import ScheduleUpdate, ScheduleUpdateInput

            async def _replace(inp: ScheduleUpdateInput) -> ScheduleUpdate:
                # The update REPLACES the whole schedule (including its
                # state); the paused flag is whatever the caller passed in
                # the new ScheduleState.
                return ScheduleUpdate(schedule=schedule)

            handle = client.get_schedule_handle(spec.schedule_id)
            await handle.update(_replace)
        else:
            raise
    return {
        "schedule_id": spec.schedule_id,
        "cron": spec.cron,
        "template": spec.template,
        "objective": objective,
        "created": True,
    }


async def list_schedules() -> list[dict]:
    """All schedules, newest first."""
    client = await _client()
    out: list[dict] = []
    # temporalio's list_schedules is an async generator wrapped in a
    # coroutine — await it before iterating. The yielded items expose the
    # schedule id as `id` and the paused flag under `schedule.state.paused`.
    async for schedule in await client.list_schedules():
        out.append({
            "schedule_id": schedule.id,
            "paused": schedule.schedule.state.paused,
        })
    return out


async def delete_schedule(schedule_id: str) -> bool:
    """Delete a schedule. Returns False when it did not exist."""
    client = await _client()
    handle = client.get_schedule_handle(schedule_id)
    try:
        await handle.delete()
        return True
    except Exception as exc:
        if getattr(getattr(exc, "status", None), "name", "") == "NOT_FOUND":
            return False
        raise


async def pause_schedule(schedule_id: str, paused: bool = True) -> dict:
    """Pause (or resume) a schedule. Idempotent."""
    client = await _client()
    handle = client.get_schedule_handle(schedule_id)
    if paused:
        await handle.pause()
    else:
        await handle.unpause()
    return {"schedule_id": schedule_id, "paused": paused}
