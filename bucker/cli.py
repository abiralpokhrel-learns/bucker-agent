"""bucker CLI — start tasks, inspect streams, prove durability.

    uv run python -m bucker.cli migrate
    uv run python -m bucker.cli start --objective "demo"
    uv run python -m bucker.cli start --crash-at transform
    uv run python -m bucker.cli start --code --objective "add a subtract fn to calc.py"
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
                "INSERT INTO tasks (id, task_type, objective, status, verifier, budget_usd) "
                "VALUES ($1, $2, $3, 'pending', $4, $5)",
                task_id,
                args.task_type,
                args.objective,
                args.verifier,
                args.budget_usd,
            )
        await store.append(
            task_id,
            "TaskCreated",
            {
                "objective": args.objective,
                "task_type": args.task_type,
                "verifier": args.verifier,
                "budget_usd": args.budget_usd,
            },
            idempotency_key=f"{task_id}:created",
        )
    finally:
        await pool.close()

    client = await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )

    if args.code:
        # The real pipeline: planner -> worker -> verifier -> retry/escalate.
        from bucker.workflows.code_task_workflow import (
            CodeTaskInput,
            CodeTaskWorkflow,
        )

        handle = await client.start_workflow(
            CodeTaskWorkflow.run,
            CodeTaskInput(
                task_id=str(task_id),
                objective=args.objective,
                max_retries=args.max_retries,
                budget_usd=args.budget_usd,
                deadline_minutes=args.deadline_minutes,
                adaptive=args.adaptive,
            ),
            id=f"task-{task_id}",
            task_queue=settings.task_queue,
        )
    else:
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
    workflow_label = (
        "CodeTaskWorkflow (plan->work->verify)" if args.code
        else "TaskWorkflow (demo)"
    )
    print(f"workflow     {workflow_label}")
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


# ------------------------------------------------------------- new commands --


async def cmd_tasks(args: argparse.Namespace) -> int:
    """List recent tasks: id, status, cost, tokens."""
    from bucker.core.tasks import list_tasks

    pool = await create_pool(settings.database_url)
    try:
        tasks = await list_tasks(pool, limit=args.limit)
        if args.status:
            tasks = [t for t in tasks if t["status"] == args.status]
        if not tasks:
            print("no tasks yet")
            return 0
        print(f"{'id':<10} {'status':<18} {'cost':>8} {'tokens':>8}  objective")
        for t in tasks:
            print(f"{t['task_id'][:8]:<10} {t['status']:<18} "
                  f"${t['cost_usd']:>7.4f} {t['total_tokens']:>8}  "
                  f"{t['objective'][:56]}")
        return 0
    finally:
        await pool.close()


async def cmd_usage(args: argparse.Namespace) -> int:
    """Token + cost usage: totals, by model, by stage, per day."""
    pool = await create_pool(settings.database_url)
    try:
        async with pool.acquire() as conn:
            total = await conn.fetchrow(
                "SELECT COALESCE(SUM(total_tokens),0) AS tokens, "
                "COALESCE(SUM(cost_usd),0) AS cost, COUNT(*) AS calls "
                "FROM telemetry WHERE model_used IS NOT NULL"
            )
            by_model = await conn.fetch(
                "SELECT model_used, COUNT(*) AS calls, SUM(total_tokens) AS tokens, "
                "SUM(cost_usd) AS cost FROM telemetry WHERE model_used IS NOT NULL "
                "GROUP BY model_used ORDER BY tokens DESC"
            )
            by_purpose = await conn.fetch(
                "SELECT purpose, SUM(total_tokens) AS tokens, SUM(cost_usd) AS cost "
                "FROM telemetry WHERE purpose IS NOT NULL "
                "GROUP BY purpose ORDER BY tokens DESC"
            )
        print(f"total        {int(total['tokens'] or 0):>10} tokens   "
              f"${float(total['cost'] or 0):.4f}   {int(total['calls'] or 0)} calls")
        print("\nby model:")
        for r in by_model:
            print(f"  {r['model_used']:<40} {int(r['tokens'] or 0):>10} tok  "
                  f"${float(r['cost'] or 0):.4f}  ({r['calls']} calls)")
        print("\nby stage:")
        for r in by_purpose:
            print(f"  {r['purpose'] or '?':<12} {int(r['tokens'] or 0):>10} tok  "
                  f"${float(r['cost'] or 0):.4f}")
        return 0
    finally:
        await pool.close()


async def cmd_replay(args: argparse.Namespace) -> int:
    """Deterministic re-run from recordings; report match/mismatch."""
    from bucker.core.blob import BlobStore
    from bucker.replay.engine import ReplayError, replay_task

    pool = await create_pool(settings.database_url)
    try:
        store = EventStore(pool)
        blobs = BlobStore(settings.blob_root)
        result = await replay_task(UUID(args.task_id), store=store, blobs=blobs)
        print(f"match:      {result.match}")
        print(f"original:   {'PASSED' if result.original_passed else 'FAILED'}")
        print(f"replay:     {'PASSED' if result.replayed_passed else 'FAILED'}")
        print(f"events:     {result.original_events}")
        print(result.diagnostics)
        return 0 if result.match else 1
    except ReplayError as exc:
        print(f"replay error: {exc}", file=sys.stderr)
        return 2
    finally:
        await pool.close()


async def cmd_templates(args: argparse.Namespace) -> int:
    from bucker.templates import list_templates

    for t in list_templates():
        print(f"{t['id']:<16} {t['name']:<22} "
              f"{t.get('default_budget_usd') or 'default':>8} USD  {t['description'][:60]}")
    return 0


async def cmd_schedules_list(args: argparse.Namespace) -> int:
    from bucker.core.schedules import list_schedules

    try:
        schedules = await list_schedules()
    except Exception as exc:  # noqa: BLE001
        print(f"Temporal unreachable: {type(exc).__name__}: {str(exc)[:120]}",
              file=sys.stderr)
        return 2
    if not schedules:
        print("no schedules yet — `bucker schedules create <id> --template <t>`")
        return 0
    for s in schedules:
        print(f"{s['schedule_id']:<30} {'paused' if s['paused'] else 'active'}")
    return 0


async def cmd_schedules_create(args: argparse.Namespace) -> int:
    from bucker.core.schedules import ScheduleSpec, create_schedule
    from bucker.templates import UnknownTemplateError

    try:
        result = await create_schedule(ScheduleSpec(
            schedule_id=args.schedule_id,
            cron=args.cron,
            template=args.template,
            objective=args.objective,
            budget_usd=args.budget_usd,
            deadline_minutes=args.deadline_minutes,
        ))
    except UnknownTemplateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"Temporal unreachable: {type(exc).__name__}: {str(exc)[:120]}",
              file=sys.stderr)
        return 2
    print(f"schedule {result['schedule_id']} created — {result['cron']} "
          f"({result['template']})")
    return 0


async def cmd_schedules_delete(args: argparse.Namespace) -> int:
    from bucker.core.schedules import delete_schedule

    try:
        deleted = await delete_schedule(args.schedule_id)
    except Exception as exc:  # noqa: BLE001
        print(f"Temporal unreachable: {type(exc).__name__}: {str(exc)[:120]}",
              file=sys.stderr)
        return 2
    print(f"schedule {args.schedule_id} deleted" if deleted
          else f"schedule {args.schedule_id} not found")
    return 0 if deleted else 1


async def cmd_schedules_pause(args: argparse.Namespace) -> int:
    from bucker.core.schedules import pause_schedule

    try:
        result = await pause_schedule(args.schedule_id, paused=not args.resume)
    except Exception as exc:  # noqa: BLE001
        print(f"Temporal unreachable: {type(exc).__name__}: {str(exc)[:120]}",
              file=sys.stderr)
        return 2
    print(f"schedule {result['schedule_id']} "
          f"{'paused' if result['paused'] else 'resumed'}")
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
    s.add_argument("--code", action="store_true",
                   help="run the real pipeline (plan -> work -> verify -> retry/escalate) "
                        "instead of the 5-step demo workflow")
    s.add_argument("--budget-usd", type=float, default=None,
                   help="hard cost ceiling (code tasks only)")
    s.add_argument("--deadline-minutes", type=int, default=None,
                   help="hard time ceiling (code tasks only)")
    s.add_argument("--max-retries", type=int, default=2,
                   help="verification retries before human review (code tasks only)")
    s.add_argument("--adaptive", action="store_true",
                   help="M3: vary retry strategy on repeated failure — switch model, "
                        "chunk the objective, or ask for clarification (code tasks only)")
    s.add_argument("--crash-at", default=None,
                   help="inject a hard crash at this step (demo durability test)")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(func=cmd_start)

    sh = sub.add_parser("show", help="reconstructed state for a task")
    sh.add_argument("task_id")
    sh.set_defaults(func=cmd_show)

    ev = sub.add_parser("events", help="print a task's event stream")
    ev.add_argument("task_id")
    ev.set_defaults(func=cmd_events)

    ls = sub.add_parser("tasks", help="list recent tasks (id, status, cost, tokens)")
    ls.add_argument("--limit", type=int, default=20)
    ls.add_argument("--status", default=None, help="filter by status")
    ls.set_defaults(func=cmd_tasks)

    us = sub.add_parser("usage", help="token and cost usage by model / stage / day")
    us.set_defaults(func=cmd_usage)

    rp = sub.add_parser("replay", help="deterministic re-run of a task from recordings")
    rp.add_argument("task_id")
    rp.set_defaults(func=cmd_replay)

    sc = sub.add_parser("schedules", help="list, create, pause or delete schedules")
    sc_sub = sc.add_subparsers(dest="schedule_cmd", required=True)
    sc_list = sc_sub.add_parser("list", help="list schedules")
    sc_list.set_defaults(func=cmd_schedules_list)
    sc_add = sc_sub.add_parser("create", help="create a schedule from a template")
    sc_add.add_argument("schedule_id")
    sc_add.add_argument("--cron", default="0 9 * * *", help="5-field cron expression")
    sc_add.add_argument("--template", default="code-fix", help="task template id")
    sc_add.add_argument("--objective", default="", help="override the template's objective")
    sc_add.add_argument("--budget-usd", type=float, default=None)
    sc_add.add_argument("--deadline-minutes", type=int, default=None)
    sc_add.set_defaults(func=cmd_schedules_create)
    sc_del = sc_sub.add_parser("delete", help="delete a schedule")
    sc_del.add_argument("schedule_id")
    sc_del.set_defaults(func=cmd_schedules_delete)
    sc_pause = sc_sub.add_parser("pause", help="pause (or resume) a schedule")
    sc_pause.add_argument("schedule_id")
    sc_pause.add_argument("--resume", action="store_true")
    sc_pause.set_defaults(func=cmd_schedules_pause)

    tp = sub.add_parser("templates", help="list task templates")
    tp.set_defaults(func=cmd_templates)

    return p


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(args.func(args)))


if __name__ == "__main__":
    main()
