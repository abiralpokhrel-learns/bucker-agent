"""Sweeper tests (bucker.core.sweep).

Hermetic on SQLite: seeded task rows with backdated created_at values
drive staleness deterministically — no sleeps, no clocks to race.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from bucker.core.eventstore import EventStore, create_pool


@pytest.fixture
def lite_db(tmp_path):
    def _dsn(name="sweep.db"):
        return f"sqlite:///{(tmp_path / name).as_posix()}"
    return _dsn


async def make_pool(dsn):
    return await create_pool(dsn)


async def seed_task(pool, *, status="running", age_minutes=60,
                    budget_usd=None, objective="stale thing"):
    tid = uuid4()
    created = (
        datetime.now(UTC) - timedelta(minutes=age_minutes)
    ).isoformat()
    await pool.execute(
        "INSERT INTO tasks (id, task_type, objective, status, budget_usd, "
        "created_at) VALUES ($1, 'code_change', $2, $3, $4, $5)",
        tid, objective, status, budget_usd, created,
    )
    return str(tid)


async def seed_spend(pool, store, tid: str, cost_usd: float):
    """One model-call event + its telemetry row carrying ``cost_usd``."""
    real_id = uuid.UUID(tid)
    event = await store.append(
        real_id, "ModelCallCompleted", {"purpose": "worker"},
        idempotency_key=f"{tid}:spend-{cost_usd}",
    )
    await pool.execute(
        "INSERT INTO telemetry (event_id, task_id, model_used, purpose, "
        "cost_usd, total_tokens) VALUES ($1, $2, 'test/model', 'worker', "
        "$3, $4)",
        event.id, real_id, cost_usd,
        int(cost_usd * 100_000),
    )


# ------------------------------------------------------------- detection ----


async def test_find_stale_returns_only_old_active_tasks(lite_db):
    from bucker.core.sweep import find_stale_tasks

    pool = await make_pool(lite_db())
    try:
        old_id = await seed_task(pool, status="running", age_minutes=120)
        fresh_id = await seed_task(pool, status="running", age_minutes=1)
        done_id = await seed_task(pool, status="completed", age_minutes=999)

        stale = await find_stale_tasks(pool, stale_minutes=30)
        ids = {t["task_id"] for t in stale}
        assert old_id in ids
        assert fresh_id not in ids
        assert done_id not in ids  # terminal verdicts are never stale
        entry = next(t for t in stale if t["task_id"] == old_id)
        assert entry["status"] == "running"
        assert entry["age_minutes"] >= 119
    finally:
        await pool.close()


async def test_near_budget_flags_ratio_crossing(lite_db):
    from bucker.core.sweep import find_near_budget_tasks

    pool = await make_pool(lite_db())
    store = EventStore(pool)
    try:
        hot = await seed_task(pool, status="running", age_minutes=5,
                              budget_usd=1.0)
        cold = await seed_task(pool, status="running", age_minutes=5,
                               budget_usd=1.0)
        unbudgeted = await seed_task(pool, status="running", age_minutes=5)
        await seed_spend(pool, store, hot, 0.9)     # 90% of budget
        await seed_spend(pool, store, cold, 0.3)    # 30%
        await seed_spend(pool, store, unbudgeted, 5.0)  # no ceiling: skip

        near = await find_near_budget_tasks(pool, threshold=0.8)
        ids = [t["task_id"] for t in near]
        assert ids == [hot]
        assert near[0]["budget_ratio"] == pytest.approx(0.9)
    finally:
        await pool.close()


# ----------------------------------------------------------------- halting ----


async def test_halt_appends_event_and_flips_status(lite_db):
    from bucker.core.sweep import halt_task

    pool = await make_pool(lite_db())
    store = EventStore(pool)
    try:
        tid = await seed_task(pool, status="pending", age_minutes=500)

        result = await halt_task(store, pool, tid,
                                 reason="stale 500min in 'pending'")
        assert result["halted"] is True

        events = await store.read_stream(uuid.UUID(tid))
        failure = [e for e in events if e.event_type == "TaskFailed"]
        assert len(failure) == 1
        assert failure[0].payload["reason"].startswith("sweeper:")

        status = await pool.fetchval(
            "SELECT status FROM tasks WHERE id = $1",
            uuid.UUID(tid),
        )
        assert status == "failed"
    finally:
        await pool.close()


async def test_halt_is_idempotent_per_reason(lite_db):
    """Sweeping twice must not double-record the halt (the event store's
    exactly-once index is what makes repeated sweeps safe)."""
    from bucker.core.sweep import halt_task

    pool = await make_pool(lite_db())
    store = EventStore(pool)
    try:
        tid = await seed_task(pool, status="running", age_minutes=100)
        await halt_task(store, pool, tid, reason="stale")
        await halt_task(store, pool, tid, reason="stale")

        events = await store.read_stream(uuid.UUID(tid))
        failures = [e for e in events if e.event_type == "TaskFailed"]
        assert len(failures) == 1
    finally:
        await pool.close()


async def test_run_sweep_halt_flag_populates_report(lite_db):
    from bucker.core.sweep import run_sweep

    pool = await make_pool(lite_db())
    store = EventStore(pool)
    try:
        stale_id = await seed_task(pool, status="pending", age_minutes=90)
        report = await run_sweep(pool, store, stale_minutes=30,
                                 halt_stale=True)
        assert [h["task_id"] for h in report["halted"]] == [stale_id]
        assert report["actionable"] is True
        status = await pool.fetchval(
            "SELECT status FROM tasks WHERE id = $1",
            uuid.UUID(stale_id),
        )
        assert status == "failed"
    finally:
        await pool.close()


async def test_run_sweep_clean_store_is_not_actionable(lite_db):
    from bucker.core.sweep import run_sweep

    pool = await make_pool(lite_db())
    store = EventStore(pool)
    try:
        await seed_task(pool, status="completed", age_minutes=10)
        report = await run_sweep(pool, store, stale_minutes=30)
        assert report["stale"] == []
        assert report["near_budget"] == []
        assert report["halted"] == []
        assert report["actionable"] is False
    finally:
        await pool.close()


# -------------------------------------------------------------- rendering ----


def test_format_sweep_report_sections():
    from bucker.core.sweep import format_sweep_report

    report = {
        "near_budget_threshold": 0.8,
        "stale": [{
            "task_id": "aaaaaaaa-0000",
            "task_type": "code_change",
            "status": "running",
            "objective": "do the thing",
            "budget_usd": None,
            "cost_usd": 0.0,
            "age_minutes": 91.2,
            "created_at": "x",
        }],
        "near_budget": [],
        "halted": [],
    }
    text = format_sweep_report(report)
    assert "stale tasks (1)" in text
    assert "running" in text
    assert "91.2min" in text


def test_build_sweep_message_counts():
    from bucker.core.sweep import build_sweep_message

    clean = build_sweep_message({"stale": [], "near_budget": [],
                                 "halted": []})
    assert "nothing actionable" in clean
    messy = build_sweep_message({
        "stale": [1], "near_budget": [], "halted": [{"task_id": "x"}],
        "near_budget_threshold": 0.8,
    })
    assert "1 stale" in messy and "1 halted" in messy


def test_sweep_report_json_serializable(lite_db):
    """--format json must produce valid JSON (cron consumers parse it)."""
    from bucker.core.sweep import run_sweep

    async def _run():
        pool = await make_pool(lite_db())
        try:
            return await run_sweep(pool, EventStore(pool))
        finally:
            await pool.close()

    report = asyncio.run(_run())
    encoded = json.dumps(report, default=str)
    assert "stale" in json.loads(encoded)
