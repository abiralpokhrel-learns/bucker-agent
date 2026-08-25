"""Cost/token forecast from your own recorded telemetry.

Answers the question every operator asks before launching a batch of
tasks: "what does this kind of task usually cost ME?" — answered from
THIS deployment's telemetry, not from a price sheet, because prompt
sizes, retry rates and model mix dominate real spend.

Honest by construction: only terminal tasks are folded into the
distribution (a half-spent running task would drag the averages), and
percentiles are computed in Python over per-task aggregates rather than
in SQL — SQLite has no percentile_cont, and lite mode must produce the
same numbers as Postgres.
"""

from __future__ import annotations

from typing import Any

TERMINAL_STATUSES = (
    "completed", "failed", "halted", "needs_human_review",
    "human_approved", "human_rejected", "cancelled",
)


def percentile(sorted_values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile over a PRE-SORTED list. Pure."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * (pct / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


async def forecast_by_task_type(pool: Any) -> dict:
    """Per-task_type spend distribution over terminal tasks. One query,
    all statistics derived in Python for cross-backend portability."""
    rows = await pool.fetch(
        """
        SELECT t.task_type AS task_type,
               t.status    AS status,
               t.id        AS task_id,
               COALESCE(SUM(tm.cost_usd), 0)  AS cost_usd,
               COALESCE(SUM(tm.total_tokens), 0) AS total_tokens
        FROM tasks t
        LEFT JOIN telemetry tm ON tm.task_id = t.id
        GROUP BY t.id
        """
    )

    by_type: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_type.setdefault(row["task_type"], {
            "task_type": row["task_type"],
            "n_tasks": 0,
            "by_status": {},
            "_costs": [],
            "_tokens": [],
        })
        entry["n_tasks"] += 1
        status = row["status"]
        entry["by_status"][status] = entry["by_status"].get(status, 0) + 1
        if status in TERMINAL_STATUSES:
            entry["_costs"].append(float(row["cost_usd"] or 0))
            entry["_tokens"].append(int(row["total_tokens"] or 0))

    forecasts: list[dict[str, Any]] = []
    for entry in by_type.values():
        costs = sorted(entry.pop("_costs"))
        tokens = sorted(entry.pop("_tokens"))
        completed = entry["by_status"].get("completed", 0)
        human_ok = entry["by_status"].get("human_approved", 0)
        terminal = sum(
            n for s, n in entry["by_status"].items() if s in TERMINAL_STATUSES
        )
        good = completed + human_ok
        forecasts.append({
            "task_type": entry["task_type"],
            "n_tasks": entry["n_tasks"],
            "terminal": terminal,
            "success_rate": round(good / terminal, 3) if terminal else None,
            "avg_cost_usd": round(sum(costs) / len(costs), 6) if costs else None,
            "p50_cost_usd": round(percentile(costs, 50), 6) if costs else None,
            "p90_cost_usd": round(percentile(costs, 90), 6) if costs else None,
            "max_cost_usd": max(costs) if costs else None,
            "avg_tokens": int(sum(tokens) / len(tokens)) if tokens else None,
            "p90_tokens": int(percentile(tokens, 90)) if tokens else None,
            "by_status": entry["by_status"],
        })

    forecasts.sort(key=lambda f: -(f["n_tasks"]))
    return {
        "forecast": forecasts,
        "note": "distributions cover TERMINAL tasks only; success_rate "
                "counts human_approved as success",
    }


def format_forecast(payload: dict) -> str:
    """Table rendering for the CLI (pure)."""
    lines = [
        f"{'task type':<14} {'n':>5} {'ok%':>6} {'avg$':>9} "
        f"{'p90$':>9} {'max$':>9} {'avg tok':>9}"
    ]
    for f in payload["forecast"]:
        ok = f"{f['success_rate']:.0%}" if f["success_rate"] is not None else "-"
        avg = f"{f['avg_cost_usd']:.4f}" if f["avg_cost_usd"] is not None else "-"
        p90 = f"{f['p90_cost_usd']:.4f}" if f["p90_cost_usd"] is not None else "-"
        mx = f"{f['max_cost_usd']:.4f}" if f["max_cost_usd"] is not None else "-"
        toks = str(f["avg_tokens"]) if f["avg_tokens"] is not None else "-"
        lines.append(
            f"{f['task_type']:<14} {f['n_tasks']:>5} {ok:>6} {avg:>9} "
            f"{p90:>9} {mx:>9} {toks:>9}"
        )
    if len(lines) == 1:
        lines.append("(no tasks yet)")
    return "\n".join(lines)
