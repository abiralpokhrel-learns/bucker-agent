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

from bucker.config import settings
from bucker.core.eventstore import EventStore, create_pool
from bucker.core.snapshots import SnapshotStore
from bucker.workflows.task_workflow import TaskWorkflow, TaskWorkflowInput

#: Project .env — the setup wizard's default write target.
ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def _full_stack_hint(command: str, http_path: str, error: str = "") -> str:
    """The "you're not on the full stack" message for CLI commands that
    hardwire Postgres/Temporal.

    Lite mode is a first-class, documented path — a CLI command that
    needs the full stack must say so plainly instead of leaking a raw
    asyncpg/temporalio traceback (the reviewer-reported gap: `bucker
    start` and `bucker graph run` crashed with ConnectionRefusedError
    while the equivalent HTTP endpoints on the same server worked fine).
    """
    hint = (
        f"[ERROR] `bucker {command}` needs the full stack (Temporal + "
        "Postgres), but this process is in lite mode (SQLite / local "
        "sandbox) or the stack is not reachable.\n"
        "        Lite mode has its own HTTP API — use it instead:\n"
        f"          curl -X POST http://localhost:8123{http_path}\n"
        "        Full stack:  uv sync --extra full && "
        "uv run python -m bucker.cli dev"
    )
    if error:
        hint += f"\n        (underlying error: {error})"
    return hint


def _require_full_stack(command: str, http_path: str) -> bool:
    """True when the caller may continue; prints the hint and returns
    False when lite mode is detected (SQLite DSN or local sandbox)."""
    if not (
        settings.database_url.startswith("sqlite:")
        or settings.sandbox_mode == "local"
    ):
        return True
    print(_full_stack_hint(command, http_path), file=sys.stderr)
    return False


# ------------------------------------------------------------- commands ----
async def cmd_migrate(args: argparse.Namespace) -> int:
    """Apply migrations as the OWNER role (not bucker_app, which cannot DDL)."""
    import asyncpg  # full stack only (bucker[full])

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


async def cmd_reconcile(args: argparse.Namespace) -> int:
    """Re-schedule registered tasks whose workflow never started."""
    from bucker.core.eventstore import EventStore, create_pool
    from bucker.core.tasks import reconcile_pending

    pool = await create_pool(settings.database_url)
    try:
        report = await reconcile_pending(
            pool, EventStore(pool),
            dry_run=args.dry_run,
            max_age_minutes=args.max_age_minutes,
        )
    finally:
        await pool.close()
    print(f"found {report['found']} unscheduled task(s) "
          f"({args.max_age_minutes}+ min old)")
    if args.dry_run:
        for tid in report["skipped_dry_run"]:
            print(f"  would re-schedule {tid[:8]}")
        print("dry run — nothing started")
        return 0
    print(f"scheduled {report['scheduled']}")
    for failure in report["failed"]:
        print(f"  FAILED {failure['task_id'][:8]}: {failure['error'][:120]}")
    return 0 if not report["failed"] else 1


async def cmd_setup(args: argparse.Namespace) -> int:
    """One-command environment bootstrap (checks, fixes, .env, database)."""
    from bucker.dev import run_setup

    return await run_setup()


async def cmd_dev(args: argparse.Namespace) -> int:
    """The ONE command: first run bootstraps (prereqs, .env, Postgres,
    migrations), then starts the whole local stack in one terminal."""
    from bucker.dev import _print_plan, first_run_needed, plan_stack, run_dev

    if args.dry_run:
        print("bucker dev --dry-run: what WOULD happen")
        _print_plan(plan_stack())
        needs = await first_run_needed()
        print(f"  first-run setup needed : {'yes' if needs else 'no'}")
        return 0
    return await run_dev(
        live_models=not args.no_live,
        force_setup=args.force_setup,
        open_browser=not args.no_browser,
    )


async def cmd_lite(args: argparse.Namespace) -> int:
    """Zero-infrastructure mode: Python only, sqlite storage, in-process
    runner, local sandbox. No Docker, no Postgres, no Temporal, no uv."""
    from bucker.lite import run_lite

    return await run_lite(
        open_browser=not args.no_browser,
        port=args.port,
    )


async def cmd_start(args: argparse.Namespace) -> int:
    if not _require_full_stack("start", "/tasks?objective=..."):
        return 2
    from temporalio.client import Client  # full stack only (bucker[full])

    task_id = uuid4()

    try:
        pool = await create_pool(settings.database_url)
    except Exception as exc:  # noqa: BLE001 — the hint matters more than the trace
        print(_full_stack_hint(
            "start", "/tasks?objective=...",
            error=f"{type(exc).__name__}: {str(exc)[:160]}",
        ), file=sys.stderr)
        return 2
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
    finally:
        await pool.close()

    if not tasks:
        print("no tasks yet")
        return 0

    if args.format == "json":
        print(json.dumps(tasks, indent=2, default=str))
        return 0
    if args.format == "csv":
        _print_csv(tasks, list(tasks[0]))
        return 0
    print(f"{'id':<10} {'status':<18} {'cost':>8} {'tokens':>8}  objective")
    for t in tasks:
        print(f"{t['task_id'][:8]:<10} {t['status']:<18} "
              f"${t['cost_usd']:>7.4f} {t['total_tokens']:>8}  "
              f"{t['objective'][:56]}")
    return 0


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
    finally:
        await pool.close()

    payload = {
        "total": {
            "tokens": int(total["tokens"] or 0),
            "cost_usd": float(total["cost"] or 0),
            "calls": int(total["calls"] or 0),
        },
        "by_model": [
            {
                "model": r["model_used"],
                "calls": int(r["calls"]),
                "tokens": int(r["tokens"] or 0),
                "cost_usd": float(r["cost"] or 0),
            }
            for r in by_model
        ],
        "by_stage": [
            {
                "purpose": r["purpose"] or "?",
                "tokens": int(r["tokens"] or 0),
                "cost_usd": float(r["cost"] or 0),
            }
            for r in by_purpose
        ],
    }

    if args.format == "json":
        print(json.dumps(payload, indent=2))
        return 0

    print(f"total        {payload['total']['tokens']:>10} tokens   "
          f"${payload['total']['cost_usd']:.4f}   "
          f"{payload['total']['calls']} calls")
    print("\nby model:")
    for r in payload["by_model"]:
        print(f"  {r['model']:<40} {r['tokens']:>10} tok  "
              f"${r['cost_usd']:.4f}  ({r['calls']} calls)")
    print("\nby stage:")
    for r in payload["by_stage"]:
        print(f"  {r['purpose']:<12} {r['tokens']:>10} tok  "
              f"${r['cost_usd']:.4f}")
    return 0


async def cmd_replay(args: argparse.Namespace) -> int:
    """Deterministic re-run from recordings; report match/mismatch.

    Single-task by default. ``--recent N`` replays a whole batch (the
    fleet-level reproducibility check the M2 evidence story needs) and
    exits non-zero on any mismatch.
    """
    from bucker.core.blob import BlobStore
    from bucker.replay.engine import ReplayError, replay_task

    if args.recent is None and not args.task_id:
        print("give a TASK_ID, or use --recent N for batch replay",
              file=sys.stderr)
        return 2

    pool = await create_pool(settings.database_url)
    try:
        store = EventStore(pool)
        blobs = BlobStore(settings.blob_root)

        if args.recent is not None:
            from bucker.replay.batch import format_batch_report, replay_batch

            report = await replay_batch(
                pool, store, blobs,
                limit=args.recent,
                status_filter=args.status or "completed",
            )
            print(format_batch_report(report))
            return 0 if not report.mismatched else 1

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


async def cmd_models(args: argparse.Namespace) -> int:
    """Browse the catalog; mark what is configured."""
    from bucker.config import settings
    from bucker.models import CATALOG, tier_of

    configured = [settings.model, *settings.model_fallbacks]
    tier_label = {"local": "local ", "free": "free  ", "paid": "paid  "}
    print(f"{'tier':<7} {'model id':<52} context  configured")
    for m in CATALOG:
        mark = "  <-- configured" if m.id in configured else ""
        print(f"{tier_label.get(m.tier, '?     ')} {m.id:<52} "
              f"{m.context:>8,}  {mark}")
    unknown = [c for c in configured if tier_of(c) == "unknown"]
    if unknown:
        print("\nconfigured but not in catalog:", ", ".join(unknown))
    print("\ncurrent chain:", " -> ".join(configured) if configured else "(empty)")
    return 0


async def cmd_providers(args: argparse.Namespace) -> int:
    """Live provider status: Ollama models + OpenRouter key shape."""
    from bucker.providers import provider_status

    status = await provider_status()
    for name, info in status["providers"].items():
        ok = "OK  " if info["ok"] else "DOWN"
        print(f"{name:<11} [{ok}] {info['detail']}")
    if status["ollama_models"]:
        print(f"\npulled locally: {', '.join(status['ollama_models'])}")
    print("\nsuggested free-first chain: " +
          (" -> ".join(status["suggested_chain"]) if status["suggested_chain"]
           else "(nothing detected — run `bucker setup-wizard` for guidance)"))
    return 0


async def cmd_setup_wizard(args: argparse.Namespace) -> int:
    """Wizard: propose a free-first chain; --apply writes it to .env."""
    from bucker.config import settings
    from bucker.providers import provider_status
    from bucker.setup import apply_env, propose_env

    status = await provider_status()
    proposal = propose_env(
        ollama_models=status["ollama_models"],
        openrouter_key_ok=status["openrouter_key_ok"],
        deepseek_key_ok=status.get("deepseek_key_ok", False),
        current_model=settings.model,
        current_fallbacks=settings.model_fallbacks,
    )

    print("bucker setup — free-first model configuration\n")
    for line in proposal["reasoning"]:
        print(f"  - {line}")

    chain = proposal["chain"]
    print(f"\nproposed chain: {' -> '.join(chain) if chain else '(none)'}")
    print("  primary   (BUCKER_MODEL)             :", proposal["primary"])
    if proposal["fallbacks"]:
        print("  fallbacks (BUCKER_MODEL_FALLBACKS)  :",
              ", ".join(proposal["fallbacks"]))
    else:
        print("  fallbacks (BUCKER_MODEL_FALLBACKS)  : (none)")

    if proposal["unchanged"]:
        print("\ncurrent .env already matches the proposal — nothing to do.")
        return 0

    if not args.apply:
        print("\nnot written (dry run). Re-run with --apply to write .env.")
        return 0

    env_path = Path(args.env) if args.env else ROOT_ENV
    changed = apply_env(proposal, env_path)
    print(f"\nwrote {env_path}:")
    for line in changed:
        print(f"  {line}")
    print("\nrestart the API/worker to pick up the new chain.")
    return 0


async def cmd_schedules_list(args: argparse.Namespace) -> int:
    if settings.database_url.startswith("sqlite:"):
        pool = await create_pool(settings.database_url)
        try:
            from bucker.lite.scheduler import list_schedules as lite_list

            schedules = await lite_list(pool)
        finally:
            await pool.close()
        if not schedules:
            print("no schedules yet — `bucker schedules create <id> --template <t>`")
            return 0
        for s in schedules:
            next_run = s.get("next_run_at") or "-"
            print(f"{s['schedule_id']:<30} "
                  f"{'paused' if s['paused'] else 'active':<8} "
                  f"{s['cron']:<16} next {next_run}")
        return 0
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
    from bucker.templates import UnknownTemplateError

    spec_args = {
        "schedule_id": args.schedule_id,
        "cron": args.cron,
        "template": args.template,
        "objective": args.objective,
        "budget_usd": args.budget_usd,
        "deadline_minutes": args.deadline_minutes,
    }
    if settings.database_url.startswith("sqlite:"):
        pool = await create_pool(settings.database_url)
        try:
            from bucker.lite.scheduler import create_schedule as lite_create

            result = await lite_create(pool, spec_args)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except UnknownTemplateError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            await pool.close()
        print(f"schedule {result['schedule_id']} created — {result['cron']} "
              f"(first run {result.get('next_run_at', '?')})")
        return 0
    from bucker.core.schedules import ScheduleSpec, create_schedule

    try:
        result = await create_schedule(ScheduleSpec(**spec_args))
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
    if settings.database_url.startswith("sqlite:"):
        pool = await create_pool(settings.database_url)
        try:
            from bucker.lite.scheduler import delete_schedule as lite_delete

            deleted = await lite_delete(pool, args.schedule_id)
        finally:
            await pool.close()
        print(f"schedule {args.schedule_id} deleted" if deleted
              else f"schedule {args.schedule_id} not found")
        return 0 if deleted else 1
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
    if settings.database_url.startswith("sqlite:"):
        pool = await create_pool(settings.database_url)
        try:
            from bucker.lite.scheduler import set_paused as lite_pause

            result = await lite_pause(pool, args.schedule_id,
                                      paused=not args.resume)
        finally:
            await pool.close()
        if result is None:
            print(f"schedule {args.schedule_id} not found", file=sys.stderr)
            return 1
        next_run = result.get("next_run_at") or "(none computed)"
        print(f"schedule {result['schedule_id']} "
              f"{'paused' if result['paused'] else 'resumed'} — next {next_run}")
        return 0
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


# ------------------------------------------------------ memory + skills + export --


async def cmd_memory_add(args: argparse.Namespace) -> int:
    from bucker.memory.facts import MemoryStore

    try:
        fact_id = MemoryStore().add(args.text, source=args.source)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"stored fact {fact_id}")
    return 0


async def cmd_memory_list(args: argparse.Namespace) -> int:
    from bucker.memory.facts import MemoryStore

    facts = MemoryStore().list()
    if not facts:
        print("no facts yet — `bucker memory add \"<durable fact>\"`")
        return 0
    for f in facts:
        print(f"[{f['id'][:8]}] ({f['source']}) {f['text']}")
    print(f"\n{len(facts)} facts")
    return 0


async def cmd_memory_search(args: argparse.Namespace) -> int:
    from bucker.memory.facts import MemoryStore

    facts = MemoryStore().search(args.query)
    if not facts:
        print(f"no facts matching {args.query!r}")
        return 1
    for f in facts:
        print(f"[{f['id'][:8]}] ({f['source']}) {f['text']}")
    return 0


async def cmd_memory_status(args: argparse.Namespace) -> int:
    """Audit the semantic-memory store: health + provenance."""
    from collections import Counter

    from bucker.memory.facts import MemoryStore

    store = MemoryStore()
    facts = store.list()
    by_source = Counter(f.get("source", "?") for f in facts)
    print(f"facts: {len(facts)}")
    for source, count in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"  {count:>4}  from {source}")
    if facts:
        print(f"oldest: {facts[-1]['created']}  newest: {facts[0]['created']}")
    else:
        print("empty — tasks consolidate automatically (BUCKER_AUTO_CONSOLIDATE)")
    print("\nhint: `bucker memory prune` dedupes identical facts and caps the store")
    return 0


async def cmd_memory_prune(args: argparse.Namespace) -> int:
    from bucker.memory.facts import MemoryStore

    store = MemoryStore()
    removed = store.prune(limit=args.limit)
    if not removed:
        print("nothing to prune — store is already clean")
        return 0
    print(f"removed {len(removed)} facts (dedupe + cap at {args.limit}):")
    for fid in removed[:10]:
        print(f"  {fid}")
    if len(removed) > 10:
        print(f"  ... and {len(removed) - 10} more")
    print(f"remaining: {store.count()}")
    return 0


async def cmd_memory_consolidate(args: argparse.Namespace) -> int:
    from bucker.memory.consolidate import consolidate_task
    from bucker.memory.facts import MemoryStore

    pool = await create_pool(settings.database_url)
    try:
        store = EventStore(pool)
        result = await consolidate_task(
            args.task_id, store, MemoryStore(), force=args.force,
        )
    finally:
        await pool.close()

    if result.already_done:
        print(f"task {args.task_id} already consolidated (--force to redo)")
        return 0
    for fact_id in result.facts_added:
        print(f"fact added: {fact_id}")
    for proposal in result.skill_proposals:
        print(f"skill proposal: {proposal['name']} — {proposal['why'][:80]}")
    if not result.facts_added and not result.skill_proposals:
        print("nothing to consolidate (task has no objective/verdict events)")
    return 0


async def cmd_skills_list(args: argparse.Namespace) -> int:
    from bucker.memory.skills import SkillStore

    skills = SkillStore().list()
    if not skills:
        print("no skills yet — `bucker skills new <name> --description ... --procedure ...`")
        return 0
    for s in skills:
        print(f"{s.name:<28} {s.description[:70]}")
    print(f"\n{len(skills)} skills")
    return 0


async def cmd_skills_show(args: argparse.Namespace) -> int:
    from bucker.memory.skills import SkillStore

    skill = SkillStore().get(args.name)
    if skill is None:
        print(f"no skill named {args.name!r}", file=sys.stderr)
        return 1
    print(f"# {skill.name}\n")
    print(skill.description + "\n")
    print(skill.body)
    return 0


async def cmd_skills_new(args: argparse.Namespace) -> int:
    from bucker.memory.skills import SkillStore

    procedure = args.procedure.replace("\\n", "\n")
    try:
        skill = SkillStore().add(args.name, args.description, procedure)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"skill {skill.name} created — the worker will apply it when an "
          f"objective mentions: {skill.description[:60]}")
    return 0


async def cmd_export(args: argparse.Namespace) -> int:
    """Export a task's trajectory: markdown, JSON, or JSONL."""
    from bucker.core.trajectory import (
        export_trajectory,
        trajectory_to_jsonl,
        trajectory_to_markdown,
    )

    pool = await create_pool(settings.database_url)
    try:
        store = EventStore(pool)
        trajectory = await export_trajectory(UUID(args.task_id), store)
    finally:
        await pool.close()

    if not trajectory["events"]:
        print(f"task {args.task_id} has no events", file=sys.stderr)
        return 1
    if args.format == "md":
        print(trajectory_to_markdown(trajectory))
    elif args.format == "jsonl":
        print(trajectory_to_jsonl(trajectory), end="")
    else:
        print(json.dumps(trajectory, indent=2, default=str))
    return 0


async def cmd_graph_run(args: argparse.Namespace) -> int:
    """Validate + launch a multi-step task DAG from a spec JSON file."""
    import json as _json

    from bucker.contracts.graph import parse_spec, validate_graph
    from bucker.core.tasks import create_task

    if not _require_full_stack("graph run", "/graphs"):
        return 2

    try:
        spec_data = _json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"spec file not found: {args.spec_file}", file=sys.stderr)
        return 2
    except _json.JSONDecodeError as exc:
        print(f"spec file is not valid JSON: {exc}", file=sys.stderr)
        return 2

    try:
        spec = parse_spec(spec_data)
    except ValueError as exc:
        print(f"invalid graph spec: {exc}", file=sys.stderr)
        return 2
    errors = validate_graph(spec)
    if errors:
        print("graph is not runnable:")
        for e in errors:
            print(f"  - {e}")
        return 2

    try:
        pool = await create_pool(settings.database_url)
    except Exception as exc:  # noqa: BLE001 — the hint matters more than the trace
        print(_full_stack_hint(
            "graph run", "/graphs",
            error=f"{type(exc).__name__}: {str(exc)[:160]}",
        ), file=sys.stderr)
        return 2
    try:
        store = EventStore(pool)
        task_id, workflow_id, schedule_error = await create_task(
            store,
            pool,
            objective=f"graph: {spec.name} ({len(spec.steps)} steps)",
            task_type="graph",
            verifier="noop",
            graph_spec=spec_data,
        )
        if workflow_id is None:
            print(f"registered but NOT scheduled: {schedule_error}", file=sys.stderr)
            return 1
    finally:
        await pool.close()

    print(f"graph launched: task {task_id}")
    print(f"  {len(spec.steps)} steps across "
          f"{len(topological_waves(spec))} parallel waves")
    if workflow_id is None:
        print("  WARNING: Temporal unreachable — task registered but not "
              "scheduled yet")
    else:
        print("  watch it: /tasks/" + task_id + "/dashboard")
    return 0


def topological_waves(spec) -> list:
    """Local import keeps the CLI lean when graphs are unused."""
    from bucker.contracts.graph import topological_waves as _tw

    return _tw(spec)


async def cmd_watch(args: argparse.Namespace) -> int:
    """Live-tail a task's event stream until it reaches a verdict."""
    from bucker.core.watch import exit_code_for

    pool = await create_pool(settings.database_url)
    try:
        from bucker.core.watch import watch_task

        store = EventStore(pool)
        snaps = SnapshotStore(pool, store)
        status = await watch_task(
            store, snaps, UUID(args.task_id),
            interval_s=args.interval,
            timeout_s=args.timeout * 60,
        )
    finally:
        await pool.close()

    if status is None:
        print(f"\ntimeout after {args.timeout} min — task still running",
              file=sys.stderr)
        return 3
    print(f"\nverdict: {status}")
    return exit_code_for(status)


async def cmd_wait(args: argparse.Namespace) -> int:
    """Block until a task finishes; print nothing but the verdict line."""
    from bucker.core.watch import exit_code_for, wait_for_status

    pool = await create_pool(settings.database_url)
    try:
        snaps = SnapshotStore(pool, EventStore(pool))
        if not args.quiet:
            existing = await snaps.get_state(UUID(args.task_id))
            if existing.get("status") is None and not existing:
                print(f"no events yet for {args.task_id} — waiting anyway",
                      file=sys.stderr)
        status = await wait_for_status(
            snaps, UUID(args.task_id),
            interval_s=args.interval,
            timeout_s=args.timeout * 60,
        )
    finally:
        await pool.close()

    if status is None:
        if not args.quiet:
            print(f"timeout after {args.timeout} min — task still running",
                  file=sys.stderr)
        return 3
    if not args.quiet:
        print(status)
    return exit_code_for(status)


# ------------------------------------------------------ output helpers ----


def _print_csv(rows: list[dict], columns: list[str]) -> None:
    """One CSV table to stdout. Pure formatting; callers own the rows."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    print(buf.getvalue(), end="")


async def cmd_version(args: argparse.Namespace) -> int:
    """Version + which mode this checkout is configured for."""
    from bucker import __version__

    mode = "lite (sqlite, in-process runner)" \
        if settings.database_url.startswith("sqlite:") else "full stack"
    verifiers = ", ".join(_verifier_names())
    print(f"bucker-agent {__version__}")
    print(f"python     {sys.version.split()[0]}")
    print(f"mode       {mode}")
    print(f"model      {settings.model} ({settings.model_mode})")
    print(f"verifiers  {verifiers}")
    return 0


def _verifier_names() -> list[str]:
    from bucker.verifiers import available, register_builtins

    register_builtins()
    return list(available())


async def cmd_sweep(args: argparse.Namespace) -> int:
    """Report stale and near-budget tasks; optionally halt and/or notify."""
    from bucker.core.sweep import (
        build_sweep_message,
        format_sweep_report,
        run_sweep,
    )

    pool = await create_pool(settings.database_url)
    try:
        store = EventStore(pool)
        report = await run_sweep(
            pool, store,
            stale_minutes=args.stale_minutes,
            near_budget_threshold=args.near_budget,
            halt_stale=args.halt,
        )
        if args.notify and report["actionable"]:
            from bucker.core.notify import deliver, is_configured

            if is_configured():
                result = await deliver(build_sweep_message(report))
                if not result.get("delivered"):
                    print(f"notify failed: {result}", file=sys.stderr)
            else:
                print("notify requested but no delivery channel is "
                      "configured", file=sys.stderr)
    finally:
        await pool.close()

    if args.format == "json":
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_sweep_report(report))
    # Cron-friendly contract: actionable findings that were NOT auto-halted
    # exit 1 so a scheduled sweep can alert; --halt resolves them instead.
    if report["actionable"] and not args.halt:
        return 1
    return 0


async def cmd_forecast(args: argparse.Namespace) -> int:
    """Cost/token distributions per task type, from recorded telemetry."""
    from bucker.core.forecast import forecast_by_task_type, format_forecast

    pool = await create_pool(settings.database_url)
    try:
        payload = await forecast_by_task_type(pool)
    finally:
        await pool.close()

    if args.format == "json":
        print(json.dumps(payload, indent=2))
        return 0
    if args.format == "csv":
        flat = []
        for f in payload["forecast"]:
            row = {k: v for k, v in f.items() if k != "by_status"}
            row.update({f"status_{s}": n for s, n in f["by_status"].items()})
            flat.append(row)
        columns = list(flat[0]) if flat else ["task_type", "n_tasks"]
        _print_csv(flat, columns)
        return 0
    print(format_forecast(payload))
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

    rec = sub.add_parser("reconcile", help="re-schedule tasks whose workflow never started")
    rec.add_argument("--dry-run", action="store_true", help="report only, start nothing")
    rec.add_argument("--max-age-minutes", type=int, default=2,
                     help="only tasks older than this (default 2)")
    rec.set_defaults(func=cmd_reconcile)

    setup_p = sub.add_parser(
        "setup", help="one-command environment bootstrap (checks, .env, database)")
    setup_p.set_defaults(func=cmd_setup)

    dev_p = sub.add_parser(
        "dev", help="THE one command: first run bootstraps, then starts "
                    "temporal + worker + dashboard in one terminal")
    dev_p.add_argument("--dry-run", action="store_true",
                       help="show what would be started, start nothing")
    dev_p.add_argument("--no-live", action="store_true",
                       help="run the worker in recorded mode (no real model calls)")
    dev_p.add_argument("--force-setup", action="store_true",
                       help="re-run setup even if the machine already looks ready")
    dev_p.add_argument("--no-browser", action="store_true",
                       help="do not auto-open the dashboard")
    dev_p.set_defaults(func=cmd_dev)

    lite_p = sub.add_parser(
        "lite", help="run the whole platform with nothing but Python — "
                     "no Docker, no Postgres, no Temporal, no uv")
    lite_p.add_argument("--no-browser", action="store_true",
                        help="do not auto-open the dashboard")
    lite_p.add_argument("--port", type=int, default=8123,
                        help="dashboard port (default 8123)")
    lite_p.set_defaults(func=cmd_lite)

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
    ls.add_argument("--format", default="table", choices=["table", "json", "csv"],
                    help="output format (default table)")
    ls.set_defaults(func=cmd_tasks)

    us = sub.add_parser("usage", help="token and cost usage by model / stage / day")
    us.add_argument("--format", default="table", choices=["table", "json"],
                    help="output format (default table)")
    us.set_defaults(func=cmd_usage)

    rp = sub.add_parser("replay", help="deterministic re-run of a task from recordings")
    rp.add_argument("task_id", nargs="?", default=None,
                    help="single task to replay (omit when using --recent)")
    rp.add_argument("--recent", type=int, default=None, metavar="N",
                    help="batch mode: replay the N most recent tasks matching "
                         "--status and report the match rate")
    rp.add_argument("--status", default="completed",
                    help="status filter for --recent (default completed)")
    rp.set_defaults(func=cmd_replay)

    sw = sub.add_parser(
        "sweep", help="report stale + near-budget tasks; optionally halt "
                      "and/or notify")
    sw.add_argument("--stale-minutes", type=int, default=30,
                    help="active tasks older than this are stale (default 30)")
    sw.add_argument("--near-budget", type=float, default=0.8,
                    help="flag running tasks at this fraction of budget "
                         "(default 0.8)")
    sw.add_argument("--halt", action="store_true",
                    help="record TaskFailed for stale tasks (append-only)")
    sw.add_argument("--notify", action="store_true",
                    help="send the digest through the configured delivery "
                         "channel(s)")
    sw.add_argument("--format", default="table", choices=["table", "json"],
                    help="output format (default table)")
    sw.set_defaults(func=cmd_sweep)

    fc = sub.add_parser(
        "forecast", help="cost/token distributions per task type from YOUR "
                         "recorded telemetry")
    fc.add_argument("--format", default="table", choices=["table", "json", "csv"],
                    help="output format (default table)")
    fc.set_defaults(func=cmd_forecast)

    ver = sub.add_parser("version", help="version + configured mode")
    ver.set_defaults(func=cmd_version)

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

    md = sub.add_parser("models", help="browse the model catalog (free/paid/local) "
                                       "and see what is configured")
    md.set_defaults(func=cmd_models)

    pv = sub.add_parser("providers", help="live provider status: Ollama models, "
                                          "OpenRouter key shape")
    pv.set_defaults(func=cmd_providers)

    su = sub.add_parser(
        "setup-wizard",
        help="model-key wizard (the old `bucker setup`): propose a free-first "
             "chain and (with --apply) write it to .env",
    )
    su.add_argument("--apply", action="store_true",
                    help="write the proposal into .env (BUCKER_MODEL + "
                         "BUCKER_MODEL_FALLBACKS only; other lines untouched)")
    su.add_argument("--env", default=None, help="path to .env (default: project .env)")
    su.set_defaults(func=cmd_setup_wizard)

    mem = sub.add_parser("memory", help="semantic memory: durable facts across sessions")
    mem_sub = mem.add_subparsers(dest="memory_cmd", required=True)
    mem_add = mem_sub.add_parser("add", help="store a fact")
    mem_add.add_argument("text")
    mem_add.add_argument("--source", default="user")
    mem_add.set_defaults(func=cmd_memory_add)
    mem_ls = mem_sub.add_parser("list", help="list all facts")
    mem_ls.set_defaults(func=cmd_memory_list)
    mem_se = mem_sub.add_parser("search", help="keyword search")
    mem_se.add_argument("query")
    mem_se.set_defaults(func=cmd_memory_search)
    mem_co = mem_sub.add_parser("consolidate", help="distill a task's run into facts")
    mem_co.add_argument("task_id")
    mem_co.add_argument("--force", action="store_true")
    mem_co.set_defaults(func=cmd_memory_consolidate)
    mem_st = mem_sub.add_parser("status", help="audit the store: counts by source")
    mem_st.set_defaults(func=cmd_memory_status)
    mem_pr = mem_sub.add_parser("prune", help="dedupe identical facts + cap the store")
    mem_pr.add_argument("--limit", type=int, default=200)
    mem_pr.set_defaults(func=cmd_memory_prune)

    sk = sub.add_parser("skills", help="procedural memory: skills the worker follows")
    sk_sub = sk.add_subparsers(dest="skill_cmd", required=True)
    sk_ls = sk_sub.add_parser("list", help="list skills")
    sk_ls.set_defaults(func=cmd_skills_list)
    sk_sh = sk_sub.add_parser("show", help="show one skill")
    sk_sh.add_argument("name")
    sk_sh.set_defaults(func=cmd_skills_show)
    sk_new = sk_sub.add_parser("new", help="create a skill")
    sk_new.add_argument("name")
    sk_new.add_argument("--description", required=True)
    sk_new.add_argument("--procedure", required=True,
                        help="the steps (\\n separates lines)")
    sk_new.set_defaults(func=cmd_skills_new)

    ex = sub.add_parser("export", help="export a task's trajectory (LLM ops trace)")
    ex.add_argument("task_id")
    ex.add_argument("--format", default="md", choices=["md", "json", "jsonl"])
    ex.set_defaults(func=cmd_export)

    gr = sub.add_parser("graph", help="run a multi-step task DAG (graph engineering)")
    gr_sub = gr.add_subparsers(dest="graph_cmd", required=True)
    gr_run = gr_sub.add_parser("run", help="run a graph spec from a JSON file")
    gr_run.add_argument("spec_file", help="path to the graph spec JSON")
    gr_run.set_defaults(func=cmd_graph_run)

    wt = sub.add_parser(
        "watch", help="live-tail a task's events until it reaches a verdict")
    wt.add_argument("task_id")
    wt.add_argument("--interval", type=float, default=1.0,
                    help="poll interval in seconds (default 1.0)")
    wt.add_argument("--timeout", type=int, default=60,
                    help="give up after this many minutes (default 60)")
    wt.set_defaults(func=cmd_watch)

    wa = sub.add_parser(
        "wait", help="block until a task finishes (script-friendly)")
    wa.add_argument("task_id")
    wa.add_argument("--interval", type=float, default=2.0,
                    help="poll interval in seconds (default 2.0)")
    wa.add_argument("--timeout", type=int, default=60,
                    help="give up after this many minutes (default 60)")
    wa.add_argument("--quiet", action="store_true",
                    help="print nothing; communicate through the exit code only")
    wa.set_defaults(func=cmd_wait)

    return p


def main() -> None:
    # Console codepages differ wildly (CI runners: cp1252; legacy cmd:
    # cp437). Printing a non-encodable character (e.g. the ⚠ emoji —
    # crashed `bucker lite` on the launcher-windows CI job) raises
    # UnicodeEncodeError and kills the command. Force UTF-8 with
    # replacement so every console print works on any codepage.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(args.func(args)))


if __name__ == "__main__":
    main()
