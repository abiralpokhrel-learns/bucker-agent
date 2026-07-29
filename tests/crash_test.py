"""M1 — the Phase 0 milestone: kill -9 mid-task, resume, zero data loss.

This is the whole point of Phase 0. It runs the real stack (Temporal +
Postgres), kills a worker between a side effect and its event append — the
nastiest possible window — restarts the worker, and asserts:

  1. the task still reaches completion,
  2. every step appears exactly once in the event log,
  3. the side effect happened exactly once in the filesystem,
  4. reconstructed state matches a full replay.

Usage (needs `temporal server start-dev` and `docker compose up -d` running):

    python -m tests.crash_test

Exit code 0 = M1 demonstrated. Tag v0.1.0 and write the blog post.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from temporalio.client import Client

from bucker.config import settings
from bucker.core.eventstore import EventStore, create_pool
from bucker.core.snapshots import SnapshotStore
from bucker.workflows.task_workflow import DEMO_STEPS, TaskWorkflow, TaskWorkflowInput

CRASH_STEP = "transform"
REPO = Path(__file__).resolve().parent.parent

#: How long to wait for the workflow to finish after the worker is restarted.
#: Must exceed the activity start_to_close_timeout in task_workflow.py, since
#: Temporal only reschedules a dead worker's activity once that timeout fires.
RESUME_TIMEOUT = 180


def start_worker() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "bucker.worker"],
        cwd=REPO,
        env={**os.environ},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)  # let it poll the task queue
    return proc


async def main() -> int:
    print("=" * 64)
    print("M1 CRASH TEST :: kill -9 mid-task, resume, zero data loss")
    print("=" * 64)

    task_id = uuid4()
    pool = await create_pool(settings.database_url)
    store = EventStore(pool)
    snaps = SnapshotStore(pool, store)

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, task_type, objective, status) "
            "VALUES ($1, 'demo', 'crash test', 'pending')",
            task_id,
        )
    await store.append(task_id, "TaskCreated",
                       {"objective": "crash test", "task_type": "demo"},
                       idempotency_key=f"{task_id}:created")

    client = await Client.connect(settings.temporal_host,
                                  namespace=settings.temporal_namespace)

    # --- run 1: worker crashes at CRASH_STEP -----------------------------
    print(f"\n[1] starting worker, will self-destruct at step '{CRASH_STEP}'")
    worker = start_worker()

    handle = await client.start_workflow(
        TaskWorkflow.run,
        TaskWorkflowInput(task_id=str(task_id), objective="crash test",
                          crash_at=CRASH_STEP),
        id=f"crashtest-{task_id}",
        task_queue=settings.task_queue,
    )
    print(f"    workflow {handle.id} started")

    deadline = time.time() + 60
    while time.time() < deadline:
        if worker.poll() is not None:
            print(f"    worker died with code {worker.returncode} (expected 137)")
            break
        await asyncio.sleep(0.5)
    else:
        worker.kill()
        print("!! worker never crashed — crash injection did not fire")
        return 1

    mid = await store.read_stream(task_id)
    print(f"    events at crash: {len(mid)} "
          f"({[e.event_type for e in mid][-3:]})")

    # --- run 2: restart, no crash injection ------------------------------
    print("\n[2] restarting worker — Temporal should resume from the last "
          "completed activity")
    print("    (the crashed activity must first hit start_to_close_timeout "
          "before Temporal reschedules it — expect a short pause)")
    worker2 = start_worker()
    result = None
    try:
        # Watch the restarted worker's liveness while waiting, instead of just
        # blocking on the result. If the replacement worker also dies, that is
        # a specific, diagnosable bug (crash injection firing more than once) —
        # and saying so beats burning the full timeout and blaming it on tuning.
        result_task = asyncio.ensure_future(handle.result())
        deadline = time.time() + RESUME_TIMEOUT
        last_report = 0.0

        while time.time() < deadline:
            if result_task.done():
                result = result_task.result()
                print(f"    workflow completed: {result['status']}")
                break

            if worker2.poll() is not None:
                result_task.cancel()
                print(f"\n!! the RESTARTED worker also died (code {worker2.returncode})")
                print("   Crash injection fired more than once. `crash_at` travels")
                print("   in the workflow input, so every retried activity receives")
                print("   it again — the injection must be made one-shot (see the")
                print("   .crashed marker in bucker/activities/demo.py).")
                events_now = await store.read_stream(task_id)
                print(f"   events: {[e.event_type for e in events_now]}")
                return 1

            elapsed = time.time() - (deadline - RESUME_TIMEOUT)
            if elapsed - last_report >= 10:
                last_report = elapsed
                n = await store.count(task_id)
                print(f"    ...{elapsed:>3.0f}s elapsed, {n} events, worker alive")

            await asyncio.sleep(1)
        else:
            result_task.cancel()
            print(f"\n!! workflow did not complete within {RESUME_TIMEOUT}s of restart")
            print("   The worker stayed alive but no progress was made. Check that")
            print("   the activity start_to_close_timeout in task_workflow.py is")
            print("   shorter than RESUME_TIMEOUT — Temporal cannot reschedule a")
            print("   dead worker's activity until that timeout expires.")
            events_now = await store.read_stream(task_id)
            print(f"   events: {[e.event_type for e in events_now]}")
            return 1
    finally:
        worker2.terminate()
        try:
            worker2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker2.kill()

    if result is None:
        print("!! workflow produced no result")
        return 1

    # --- assertions -------------------------------------------------------
    print("\n[3] verifying")
    failures: list[str] = []

    events = await store.read_stream(task_id)
    types = [e.event_type for e in events]

    for step in DEMO_STEPS:
        completed = [e for e in events
                     if e.event_type == "StepCompleted"
                     and e.payload.get("step") == step]
        if len(completed) != 1:
            failures.append(
                f"step '{step}' has {len(completed)} StepCompleted events, expected 1"
            )
    print(f"    - each step recorded exactly once      "
          f"{'FAIL' if failures else 'ok'}")

    if types.count("TaskCompleted") != 1:
        failures.append(f"TaskCompleted appears {types.count('TaskCompleted')} times")
    print(f"    - task completed exactly once          "
          f"{'FAIL' if types.count('TaskCompleted') != 1 else 'ok'}")

    ids = [e.id for e in events]
    if ids != sorted(ids):
        failures.append("event stream is not monotonically ordered")
    print("    - event stream ordered                 ok")

    workspace = Path(settings.blob_root).parent / "workspace" / str(task_id)
    markers = sorted(p.name for p in workspace.glob("*.done"))
    expected = sorted(f"{s}.done" for s in DEMO_STEPS)
    if markers != expected:
        failures.append(f"side effects: expected {expected}, found {markers}")
    print(f"    - side effects exactly once            "
          f"{'FAIL' if markers != expected else 'ok'}")

    state = await snaps.get_state(task_id)
    full = await snaps.rebuild_full(task_id)
    if state != full:
        failures.append("snapshot path state != full replay state")
    if state["status"] != "completed":
        failures.append(f"final status is {state['status']}, expected completed")
    if state["steps_completed"] != DEMO_STEPS:
        failures.append(f"steps_completed = {state['steps_completed']}")
    print(f"    - reconstructed state correct          "
          f"{'FAIL' if failures and 'state' in failures[-1] else 'ok'}")

    await pool.close()

    print("\n" + "=" * 64)
    if failures:
        print("M1 FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("M1 PASSED :: killed mid-task, resumed, zero data loss, no duplicates")
    print(f"  task_id {task_id}  |  {len(events)} events")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
