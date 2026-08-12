"""Telemetry capture and query helpers (BUILD_PLAN step 31).

Every model call, tool call, and verification result gets a telemetry row.
The schema was created in 001_init.sql; this module makes it easy to write
rows alongside events and to query per-task cost with a single SQL statement.

Design:
  - Insert is separate from the event append — the event store does not know
    about telemetry, and telemetry does not couple to the event store.
  - A missing telemetry row is a data-quality gap, not a correctness bug.
    The task still runs if telemetry insert fails; it just can't answer the
    "how much did this cost" question.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    import asyncpg


async def record_model_call(
    conn: asyncpg.Connection,
    *,
    event_id: int,
    task_id: UUID,
    model: str,
    latency_ms: int,
    cost_usd: float | None,   # None = unknown (pricing metadata missing)
    purpose: str | None = None,
    usage: dict | None = None,
) -> None:
    """Record a model call in the telemetry table.

    ``usage`` is litellm's token breakdown (prompt/completion/total) — the
    router already captures it; storing it here is what lets the dashboard
    answer "how many tokens did this model burn".

    Call this AFTER the corresponding ModelCallCompleted event is appended.
    """
    usage = usage or {}
    await conn.execute(
        """
        INSERT INTO telemetry (
            event_id, task_id, model_used, latency_ms, cost_usd,
            purpose, prompt_tokens, completion_tokens, total_tokens
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id, task_id, model, latency_ms, cost_usd, purpose,
        usage.get("prompt_tokens"), usage.get("completion_tokens"),
        usage.get("total_tokens"),
    )


async def record_tool_call(
    conn: asyncpg.Connection,
    *,
    event_id: int,
    task_id: UUID,
    tool: str,
    latency_ms: int = 0,
) -> None:
    """Record a tool call (sandbox exec, diff apply, etc.) in telemetry."""
    await conn.execute(
        """
        INSERT INTO telemetry (event_id, task_id, tool_used, latency_ms)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id, task_id, tool, latency_ms,
    )


async def record_verification(
    conn: asyncpg.Connection,
    *,
    event_id: int,
    task_id: UUID,
    passed: bool,
    duration_ms: int,
) -> None:
    """Record a verification outcome in telemetry."""
    await conn.execute(
        """
        INSERT INTO telemetry (event_id, task_id, verification_result, latency_ms)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id, task_id, "passed" if passed else "failed", duration_ms,
    )


# --------------------------------------------------------------- queries ----


async def task_cost(pool: asyncpg.Pool, task_id: UUID) -> float:
    """Total cost for one task, in a single SQL statement.

    This is the DoD: per-task cost query is one SQL statement.
    """
    value = await pool.fetchval(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM telemetry WHERE task_id = $1",
        task_id,
    )
    return float(value or 0)


async def model_cost_breakdown(
    pool: asyncpg.Pool, task_id: UUID
) -> dict[str, float]:
    """Cost broken down by model (planner vs worker may use different models)."""
    rows = await pool.fetch(
        """
        SELECT model_used, SUM(cost_usd) as total
        FROM telemetry
        WHERE task_id = $1 AND model_used IS NOT NULL
        GROUP BY model_used
        """,
        task_id,
    )
    return {row["model_used"]: float(row["total"]) for row in rows}
