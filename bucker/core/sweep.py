"""Operational sweeper: find (and optionally halt) stuck tasks.

Two failure modes this module owns:

* **Stale tasks** — a task sitting in a non-terminal status far past its
  deadline. On the full stack Temporal usually resolves these; in lite
  mode a dead process leaves tasks frozen mid-run forever. Either way the
  operator needs one command that says WHICH tasks are zombies and since
  WHEN.
* **Near-budget tasks** — running tasks whose spend has crossed a
  fraction of their ceiling. The pre-spend guard halts BEFORE breach; the
  sweeper is the after-the-fact tripwire for tasks whose estimates were
  wrong, so nobody discovers an overrun on an invoice.

Halting is honest by construction: like every state change here, it is
an APPENDED event (TaskFailed with a sweeper reason) plus the
denormalized status flip — never an UPDATE that rewrites history.

The digest composes with the delivery channels: `bucker sweep --notify`
sends the same report to webhook/Telegram/Slack/Discord.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: Statuses from which a task can still move. Everything else is a verdict
#: already recorded and must never be swept.
ACTIVE_STATUSES = ("pending", "schedule_failed", "running", "in_progress")

DEFAULT_STALE_MINUTES = 30
DEFAULT_NEAR_BUDGET = 0.8


def _now() -> datetime:
    return datetime.now(UTC)


async def find_stale_tasks(
    pool: Any,
    *,
    stale_minutes: int = DEFAULT_STALE_MINUTES,
    now: datetime | None = None,
) -> list[dict]:
    """Active tasks older than ``stale_minutes``, oldest first.

    Age is measured against the task row's created_at (the pipeline has
    no stored per-task deadline column; the contract's deadline lives in
    the workflow input). The comparison parameter is produced by the same
    Python call site as every other timestamp so SQLite string ordering
    stays correct in lite mode.
    """
    from datetime import timedelta

    cutoff = ((now or _now()) - timedelta(minutes=stale_minutes)).isoformat()
    rows = await pool.fetch(
        """
        SELECT t.id, t.task_type, t.status, t.objective, t.budget_usd,
               t.created_at,
               COALESCE(SUM(tm.cost_usd), 0) AS cost_usd
        FROM tasks t
        LEFT JOIN telemetry tm ON tm.task_id = t.id
        WHERE t.status IN ($1, $2, $3, $4)
          AND t.created_at <= $5
        GROUP BY t.id
        ORDER BY t.created_at ASC
        LIMIT 200
        """,
        *ACTIVE_STATUSES,
        cutoff,
    )
    return [_stale_row(r) for r in rows]


def _stale_row(row: Any) -> dict:
    created = row["created_at"]
    created_iso = created.isoformat() if hasattr(created, "isoformat") else str(created)
    started = (
        datetime.fromisoformat(created_iso)
        if isinstance(created_iso, str) else None
    )
    age_minutes = (
        round((_now() - started).total_seconds() / 60, 1)
        if started is not None else None
    )
    return {
        "task_id": str(row["id"]),
        "task_type": row["task_type"],
        "status": row["status"],
        "objective": (row["objective"] or "")[:120],
        "budget_usd": row["budget_usd"],
        "cost_usd": float(row["cost_usd"] or 0),
        "age_minutes": age_minutes,
        "created_at": created_iso,
    }


async def find_near_budget_tasks(
    pool: Any,
    *,
    threshold: float = DEFAULT_NEAR_BUDGET,
) -> list[dict]:
    """Active tasks whose spend crossed ``threshold`` of their budget."""
    stale = await find_stale_tasks(pool, stale_minutes=0)
    near: list[dict] = []
    for task in stale:
        budget = task.get("budget_usd")
        if not budget or budget <= 0:
            continue
        ratio = task["cost_usd"] / float(budget)
        if ratio >= threshold:
            task["budget_ratio"] = round(ratio, 3)
            near.append(task)
    near.sort(key=lambda t: -t["budget_ratio"])
    return near


async def halt_task(store: Any, pool: Any, task_id: str, *,
                    reason: str) -> dict:
    """Append TaskFailed (sweeper reason) + flip the status cache.

    Idempotent per (task, reason-key): a repeated sweep with the same
    reason dedupes through the event store's idempotency index, so
    sweeping twice never double-records a halt.
    """
    from uuid import UUID

    tid = UUID(task_id)
    await store.append(
        tid,
        "TaskFailed",
        {"attempt": 0, "reason": f"sweeper: {reason}"[:500]},
        idempotency_key=f"{task_id}:sweeper-failed",
    )
    await pool.execute(
        "UPDATE tasks SET status = 'failed' WHERE id = $1", tid,
    )
    return {"task_id": task_id, "halted": True, "reason": reason}


async def run_sweep(
    pool: Any,
    store: Any,
    *,
    stale_minutes: int = DEFAULT_STALE_MINUTES,
    near_budget_threshold: float = DEFAULT_NEAR_BUDGET,
    halt_stale: bool = False,
) -> dict:
    """One sweep pass. Returns the full report; halts only when asked."""
    stale = await find_stale_tasks(pool, stale_minutes=stale_minutes)
    near_budget = await find_near_budget_tasks(
        pool, threshold=near_budget_threshold
    )

    report: dict[str, Any] = {
        "swept_at": _now().isoformat(),
        "stale_minutes": stale_minutes,
        "near_budget_threshold": near_budget_threshold,
        "stale": stale,
        "near_budget": near_budget,
        "halted": [],
    }

    if halt_stale:
        for task in stale:
            result = await halt_task(
                store, pool, task["task_id"],
                reason=f"stale {task['age_minutes']}min in "
                       f"{task['status']!r}",
            )
            report["halted"].append(result)

    report["actionable"] = bool(stale or near_budget or report["halted"])
    return report


def format_sweep_report(report: dict) -> str:
    """Human-readable sweep summary (pure)."""
    lines: list[str] = []
    stale = report["stale"]
    if stale:
        lines.append(f"stale tasks ({len(stale)}):")
        for t in stale:
            budget = ""
            if t["budget_usd"]:
                budget = f" ${t['cost_usd']:.4f}/${t['budget_usd']:.2f}"
            lines.append(
                f"  {t['task_id'][:8]} {t['status']:>16} "
                f"{t['age_minutes']:>7}min{budget}  {t['objective'][:48]}"
            )
    else:
        lines.append("stale tasks: none")
    near = report["near_budget"]
    if near:
        lines.append(f"near-budget tasks (>= {report['near_budget_threshold']:.0%}):")
        for t in near:
            lines.append(
                f"  {t['task_id'][:8]} {t['budget_ratio']:>5.0%} of budget "
                f"${t['cost_usd']:.4f}/${t['budget_usd']:.2f}"
            )
    halted = report["halted"]
    if halted:
        lines.append(f"halted now ({len(halted)}):")
        for h in halted:
            lines.append(f"  {h['task_id'][:8]} — {h['reason']}")
    elif not report["near_budget"]:
        lines.append("nothing actionable")
    return "\n".join(lines)


def build_sweep_message(report: dict) -> str:
    """Compact digest for delivery channels (pure)."""
    n_stale = len(report["stale"])
    n_near = len(report["near_budget"])
    n_halt = len(report["halted"])
    parts: list[str] = []
    if n_stale:
        parts.append(f"{n_stale} stale")
    if n_near:
        parts.append(f"{n_near} near-budget")
    if n_halt:
        parts.append(f"{n_halt} halted")
    if not parts:
        return "🧹 bucker sweep: nothing actionable"
    return f"🧹 bucker sweep: {', '.join(parts)}"
