"""bucker CLI — start tasks, inspect streams, prove durability.

    uv run python -m bucker.cli migrate
    uv run python -m bucker.cli start --objective "demo"
    uv run python -m bucker.cli start --crash-at transform
    uv run python -m bucker.cli show <task_id>
    uv run python -m bucker.cli events <task_id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from temporalio.client import Client

from bucker.config import settings
from bucker.core.eventstore import EventStore, create_pool
from bucker.core.snapshots import SnapshotStore
from bucker.workflows.task_workflow import TaskWorkflow, TaskWorkflowInput

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


# ------------------------------------------------------------- commands ----
async def cmd_migrate(args: argparse.Namespace) -> int:
    """Apply migrations as the OWNER role (not bucker_app, which cannot DDL)."""
    dsn = args.admin_url
    conn = await asyncpg.connect(dsn)
    try:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            print(f"applying {path.name} ...")
            await conn.execute(path.read_text())
        print("migrations applied")
    finally:
        await conn.close()
    return 0


async def cmd_start(args: argparse.Namespace) -> int:
    task_id = uuid4()

    pool = await create_pool(settings.database_url)
    store = EventStore(pool)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, task_type, objective, status, verifier) "
                "VALUES ($1, $2, $3, 'pending', $4)",
                task_id,
                args.task_type,
                args.objective,
                args.verifier,
            )
        await store.append(
            task_id,
            "TaskCreated",
            {
                "objective": args.objective,
                "task_type": args.task_type,
                "verifier": args.verifier,
                "budget_usd": settings.default_budget_usd,
            },
            idempotency_key=f"{task_id}:created",
        )
    finally:
        await pool.close()

    client = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )
    handle = await client.start_workflow(
        TaskWorkflow.run,
        TaskWorkflowInput(
            task_id=str(task_id),
            objective=args.objective,
            task_type=args.task_type,
            crash_at=args.crash_at,
        ),
        id=f"task-{task_id}",
        task_queue=settings.task_queue,
    )
    print(f"task_id      {task_id}")
    print(f"workflow_id  {handle.id}")
    print(f"ui           http://localhost:8233/namespaces/default/workflows/{handle.id}")

    if args.wait:
        result = await handle.result()
        print(json.dumps(result, indent=2))
    return 0


async def cmd_show(args: argparse.Namespace) -> int:
    pool = await create_pool(settings.database_url)
    try:
        store = EventStore(pool)
        snaps = SnapshotStore(pool, store)
        task_id = UUID(args.task_id)
        state = await snaps.get_state(task_id)
        full = await snaps.rebuild_full(task_id)
        print(json.dumps(state, indent=2, default=str))
        if state != full:
            print("\n!! SNAPSHOT DRIFT: snapshot path != full replay", file=sys.stderr)
            return 2
    finally:
        await pool.close()
    return 0


async def cmd_events(args: argparse.Namespace) -> int:
    pool = await create_pool(settings.database_url)
    try:
        store = EventStore(pool)
        events = await store.read_stream(UUID(args.task_id))
        for e in events:
            ref = f"  ref={e.tool_output_ref}" if e.tool_output_ref else ""
            print(f"{e.id:>5}  {e.created_at:%H:%M:%S}  {e.event_type:<24}"
                  f"{json.dumps(e.payload)}{ref}")
        print(f"\n{len(events)} events")
    finally:
        await pool.close()
    return 0


# ----------------------------------------------------------------- main ----
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bucker", description="bucker-agent CLI")
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("migrate", help="apply SQL migrations")
    m.add_argument(
        "--admin-url",
        default="postgresql://postgres:dev@localhost:5432/bucker",
        help="superuser DSN (migrations need DDL rights)",
    )
    m.set_defaults(func=cmd_migrate)

    s = sub.add_parser("start", help="create a task and start its workflow")
    s.add_argument("--objective", default="demo task")
    s.add_argument("--task-type", default="demo")
    s.add_argument("--verifier", default="noop")
    s.add_argument("--crash-at", default=None,
                   help="inject a hard crash at this step (durability test)")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(func=cmd_start)

    sh = sub.add_parser("show", help="reconstructed state for a task")
    sh.add_argument("task_id")
    sh.set_defaults(func=cmd_show)

    ev = sub.add_parser("events", help="print a task's event stream")
    ev.add_argument("task_id")
    ev.set_defaults(func=cmd_events)

    return p


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(args.func(args)))


if __name__ == "__main__":
    main()
