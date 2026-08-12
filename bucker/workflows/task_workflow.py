"""The durable task workflow.

[HAND] — Temporal's determinism rules apply to every line in this file.

Forbidden in workflow code: network calls, DB access, ``datetime.now()``,
``random``, ``os.environ``, threads, and any import that does I/O at module
scope. Temporal re-executes this function from the start on every replay; if it
takes a different path the second time, recovery breaks. Everything impure
belongs in an activity.

Phase 0 runs five fake steps. Phases 1+ replace them with
plan -> work -> verify while this file's shape stays the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from bucker.temporal_compat import RetryPolicy, workflow

with workflow.unsafe.imports_passed_through():
    from bucker.activities.demo import (
        StepInput,
        record_task_completed,
        record_task_started,
        run_step,
    )

DEMO_STEPS = ["fetch", "analyze", "transform", "validate", "publish"]


@dataclass
class TaskWorkflowInput:
    task_id: str
    objective: str = "demo task"
    task_type: str = "demo"
    crash_at: str | None = None


@workflow.defn
class TaskWorkflow:
    def __init__(self) -> None:
        self._current_step: str | None = None
        self._completed: list[str] = []

    @workflow.run
    async def run(self, inp: TaskWorkflowInput) -> dict:
        # Activities retry forever by default in Temporal. Bounded here so a
        # genuinely broken step surfaces instead of spinning; the workflow
        # itself still survives worker crashes regardless of this policy.
        retry = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(seconds=30),
            maximum_attempts=5,
        )
        # start_to_close_timeout is the crash-recovery dial, not just a safety
        # net. When a worker dies mid-activity, Temporal cannot know the process
        # is gone — it waits for THIS timeout to expire before rescheduling the
        # activity on another worker. Set it to 2 minutes and every crash costs
        # 2 minutes of dead air before resume begins.
        #
        # These demo steps finish in well under a second, so 15s is generous and
        # makes recovery near-instant. For genuinely long activities (a test
        # suite, a model call), do NOT just raise this — send heartbeats from
        # the activity and set heartbeat_timeout instead, so worker death is
        # detected in seconds regardless of how long the work legitimately takes.
        opts = {
            "start_to_close_timeout": timedelta(seconds=15),
            "retry_policy": retry,
        }

        await workflow.execute_activity(
            record_task_started,
            args=[inp.task_id, inp.objective, inp.task_type],
            **opts,
        )

        for index, step in enumerate(DEMO_STEPS):
            self._current_step = step
            await workflow.execute_activity(
                run_step,
                StepInput(
                    task_id=inp.task_id,
                    step=step,
                    step_index=index,
                    crash_at=inp.crash_at,
                ),
                **opts,
            )
            self._completed.append(step)

        await workflow.execute_activity(
            record_task_completed, inp.task_id, **opts
        )

        return {"task_id": inp.task_id, "steps": self._completed, "status": "completed"}

    # Queries are how you inspect a running workflow without disturbing it.
    @workflow.query
    def current_step(self) -> str | None:
        return self._current_step

    @workflow.query
    def completed_steps(self) -> list[str]:
        return list(self._completed)
