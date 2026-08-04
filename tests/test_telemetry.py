"""Telemetry tests (step 31).

Requires Postgres. Verifies that telemetry rows are written alongside
events and that the cost query works as a single SQL statement.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from bucker.core.telemetry import model_cost_breakdown, task_cost

#: Skip when Postgres is unavailable.
try:
    import asyncpg  # noqa: F401

    _DB_IMPORT_OK = True
except ImportError:
    _DB_IMPORT_OK = False


async def _postgres_reachable() -> bool:
    if not _DB_IMPORT_OK:
        return False
    from bucker.config import settings
    try:
        pool = await asyncpg.create_pool(
            settings.database_url, min_size=1, max_size=1, timeout=3,
        )
        await pool.close()
        return True
    except Exception:
        return False


_DB_REACHABLE: bool | None = None


async def _ensure_db_checked():
    global _DB_REACHABLE
    if _DB_REACHABLE is None:
        _DB_REACHABLE = await _postgres_reachable()
    return _DB_REACHABLE


def requires_db(fn):
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if not await _ensure_db_checked():
            pytest.skip("Postgres not available")
        return await fn(*args, **kwargs)

    return wrapper


# ------------------------------------------------------------ telemetry ----


@requires_db
async def test_task_cost_empty():
    """A task with no telemetry rows costs $0."""
    from bucker.config import settings
    from bucker.core.eventstore import create_pool

    pool = await create_pool(settings.database_url)
    try:
        cost = await task_cost(pool, uuid4())
        assert cost == 0.0
    finally:
        await pool.close()


@requires_db
async def test_task_cost_accumulates():
    """Insert model call + tool call + verification — cost query aggregates."""
    import asyncpg as apg

    from bucker.config import settings
    from bucker.core.eventstore import create_pool
    from bucker.core.telemetry import record_model_call, record_tool_call, record_verification

    pool = await create_pool(settings.database_url)
    tid = uuid4()

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, task_type, objective, status) "
                "VALUES ($1, 'demo', 'test', 'pending')",
                tid,
            )
            # Insert events first (they are FK targets for telemetry).
            e1 = await conn.fetchval(
                "INSERT INTO events (task_id, event_type) VALUES ($1, 'ModelCallCompleted') RETURNING id",
                tid,
            )
            e2 = await conn.fetchval(
                "INSERT INTO events (task_id, event_type) VALUES ($1, 'ToolCallCompleted') RETURNING id",
                tid,
            )
            e3 = await conn.fetchval(
                "INSERT INTO events (task_id, event_type) VALUES ($1, 'VerificationPassed') RETURNING id",
                tid,
            )

        # Use a direct connection (not the codec-set pool) so raw SQL works.
        admin_conn = await apg.connect(settings.database_url)
        try:
            await record_model_call(
                admin_conn, event_id=e1, task_id=tid,
                model="test-model", latency_ms=100, cost_usd=0.05,
            )
            await record_tool_call(
                admin_conn, event_id=e2, task_id=tid,
                tool="apply_diff",
            )
            await record_verification(
                admin_conn, event_id=e3, task_id=tid,
                passed=True, duration_ms=500,
            )
        finally:
            await admin_conn.close()

        cost = await task_cost(pool, tid)
        assert cost == pytest.approx(0.05)

        breakdown = await model_cost_breakdown(pool, tid)
        assert breakdown["test-model"] == pytest.approx(0.05)
    finally:
        await pool.close()


@requires_db
async def test_telemetry_on_conflict_does_not_double_count():
    """ON CONFLICT DO NOTHING — inserting the same event_id twice is safe."""
    import asyncpg as apg

    from bucker.config import settings
    from bucker.core.eventstore import create_pool
    from bucker.core.telemetry import record_model_call

    pool = await create_pool(settings.database_url)
    tid = uuid4()

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, task_type, objective, status) "
                "VALUES ($1, 'demo', 'test', 'pending')",
                tid,
            )
            e = await conn.fetchval(
                "INSERT INTO events (task_id, event_type) VALUES ($1, 'ModelCallCompleted') RETURNING id",
                tid,
            )

        admin_conn = await apg.connect(settings.database_url)
        try:
            await record_model_call(
                admin_conn, event_id=e, task_id=tid,
                model="m", latency_ms=10, cost_usd=0.01,
            )
            await record_model_call(
                admin_conn, event_id=e, task_id=tid,
                model="m", latency_ms=10, cost_usd=0.01,
            )
        finally:
            await admin_conn.close()

        cost = await task_cost(pool, tid)
        assert cost == pytest.approx(0.01)  # not 0.02
    finally:
        await pool.close()
