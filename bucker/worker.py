"""Temporal worker entrypoint.

Run with:  uv run python -m bucker.worker
Kill it mid-task with SIGKILL and start it again — that is the Phase 0 proof.
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from bucker.activities.demo import (
    record_task_completed,
    record_task_started,
    run_step,
)
from bucker.activities.graph import record_graph_step, register_graph_step
from bucker.activities.notify import notify_task_result
from bucker.activities.pipeline import (
    choose_adaptive_strategy,
    consolidate_task_memory,
    evaluate_policy,
    record_decision,
    run_verifier,
    run_worker,
)
from bucker.activities.planner import plan_task
from bucker.activities.schedule import register_scheduled_task
from bucker.config import settings
from bucker.workflows.code_task_workflow import CodeTaskWorkflow
from bucker.workflows.graph_workflow import GraphWorkflow
from bucker.workflows.scheduled_task_workflow import ScheduledTaskWorkflow
from bucker.workflows.task_workflow import TaskWorkflow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s :: %(message)s",
)
log = logging.getLogger("bucker.worker")


async def main() -> None:
    client = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )
    log.info(
        "worker up | temporal=%s queue=%s db=%s",
        settings.temporal_host,
        settings.task_queue,
        settings.database_url.rsplit("@", 1)[-1],
    )

    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[TaskWorkflow, CodeTaskWorkflow, ScheduledTaskWorkflow,
                   GraphWorkflow],
        activities=[
            # Phase 0 durability demo
            record_task_started, run_step, record_task_completed,
            # Phase 1 pipeline
            plan_task, run_worker, run_verifier, evaluate_policy, record_decision,
            consolidate_task_memory,
            # Phase 2 adaptive planning (M3)
            choose_adaptive_strategy,
            # Schedules (recurring tasks)
            register_scheduled_task,
            # Graphs (multi-step DAGs)
            register_graph_step,
            record_graph_step,
            notify_task_result,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("worker stopped")
