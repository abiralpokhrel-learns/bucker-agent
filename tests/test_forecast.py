"""Forecast tests (bucker.core.forecast).

The percentile math is pure and table-tested; the aggregation runs on a
seeded SQLite store to prove the SQL stays portable (no PG-only syntax)
and that non-terminal tasks are excluded from the distributions.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from bucker.core.eventstore import EventStore, create_pool

# ------------------------------------------------------------- pure math ----


@pytest.mark.parametrize("values,pct,expected", [
    ([1.0], 50, 1.0),
    ([1.0, 2.0, 3.0, 4.0], 50, 2.5),
    ([1.0, 2.0, 3.0, 4.0], 90, 3.7),
    ([10.0, 20.0], 0, 10.0),
    ([10.0, 20.0], 100, 20.0),
    ([3.0, 1.0, 2.0], 50, 2.0),
])
def test_percentile(values, pct, expected):
    from bucker.core.forecast import percentile

    assert percentile(sorted(values), pct) == pytest.approx(expected)


def test_percentile_empty_is_none():
    from bucker.core.forecast import percentile

    assert percentile([], 90) is None


# ---------------------------------------------------------- aggregation ----


@pytest.fixture
def lite_db(tmp_path):
    def _dsn(name="forecast.db"):
        return f"sqlite:///{(tmp_path / name).as_posix()}"
    return _dsn


async def seed_task_with_spend(pool, store, *, task_type="code_change",
                               status="completed", cost_usd=0.10,
                               tokens=1000):
    tid = uuid4()
    await pool.execute(
        "INSERT INTO tasks (id, task_type, objective, status) "
        "VALUES ($1, $2, 'forecast fixture task', $3)",
        tid, task_type, status,
    )
    event = await store.append(
        tid, "ModelCallCompleted", {"purpose": "worker"},
        idempotency_key=f"{tid}:call",
    )
    await pool.execute(
        "INSERT INTO telemetry (event_id, task_id, model_used, purpose, "
        "cost_usd, total_tokens) VALUES ($1, $2, 'm/x', 'worker', $3, $4)",
        event.id, tid, cost_usd, tokens,
    )
    return str(tid)


async def test_forecast_excludes_running_tasks(lite_db):
    from bucker.core.forecast import forecast_by_task_type

    pool = await create_pool(lite_db())
    store = EventStore(pool)
    try:
        await seed_task_with_spend(pool, store, status="running",
                                   cost_usd=999.0)
        await seed_task_with_spend(pool, store, status="completed",
                                   cost_usd=0.10)

        payload = await forecast_by_task_type(pool)
        entry = next(f for f in payload["forecast"]
                     if f["task_type"] == "code_change")
        assert entry["n_tasks"] == 2       # both counted as tasks...
        assert entry["terminal"] == 1      # ...but only the finished one
        assert entry["avg_cost_usd"] == pytest.approx(0.10)
        assert entry["success_rate"] == pytest.approx(1.0)
    finally:
        await pool.close()


async def test_forecast_success_counts_human_approved(lite_db):
    """human_approved is a GOOD outcome for rate purposes: the human said
    yes, the pipeline worked — it must not read as a failure."""
    from bucker.core.forecast import forecast_by_task_type

    pool = await create_pool(lite_db())
    store = EventStore(pool)
    try:
        await seed_task_with_spend(pool, store, status="failed",
                                   cost_usd=0.05)
        await seed_task_with_spend(pool, store, status="needs_human_review",
                                   cost_usd=0.05)
        await seed_task_with_spend(pool, store, status="human_approved",
                                   cost_usd=0.05)

        entry = (await forecast_by_task_type(pool))["forecast"][0]
        assert entry["terminal"] == 3  # needs_human_review IS terminal
        assert entry["by_status"]["human_approved"] == 1
        assert entry["success_rate"] == pytest.approx(round(1 / 3, 3))
    finally:
        await pool.close()


async def test_forecast_separates_task_types(lite_db):
    from bucker.core.forecast import forecast_by_task_type

    pool = await create_pool(lite_db())
    store = EventStore(pool)
    try:
        await seed_task_with_spend(pool, store, task_type="code_change",
                                   cost_usd=0.10)
        await seed_task_with_spend(pool, store, task_type="research",
                                   cost_usd=0.50)

        types = {f["task_type"]: f for f in
                 (await forecast_by_task_type(pool))["forecast"]}
        assert types["code_change"]["avg_cost_usd"] == pytest.approx(0.10)
        assert types["research"]["avg_cost_usd"] == pytest.approx(0.50)
    finally:
        await pool.close()


def test_format_forecast_renders_rows():
    from bucker.core.forecast import format_forecast

    text = format_forecast({"forecast": [{
        "task_type": "code_change", "n_tasks": 4,
        "terminal": 3, "success_rate": 0.667,
        "avg_cost_usd": 0.12, "p50_cost_usd": 0.10,
        "p90_cost_usd": 0.20, "max_cost_usd": 0.21,
        "avg_tokens": 1500, "p90_tokens": 3000,
        "by_status": {"completed": 2, "failed": 1},
    }]})
    assert "code_change" in text
    assert "67%" in text
    assert "0.1200" in text


def test_cli_forecast_json_and_csv(tmp_path, capsys):
    """`bucker forecast --format json|csv` produce machine-parseable
    output on an empty store without touching Temporal/Postgres."""
    import argparse
    import json as _json

    import bucker.config as cfg
    from bucker.cli import cmd_forecast

    dsn = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    original_dsn = cfg.settings.database_url

    async def _run(fmt):
        object.__setattr__(cfg.settings, "database_url", dsn)
        try:
            return await cmd_forecast(argparse.Namespace(format=fmt))
        finally:
            object.__setattr__(cfg.settings, "database_url", original_dsn)

    code = asyncio.run(_run("json"))
    body = _json.loads(capsys.readouterr().out)
    assert code == 0
    assert "forecast" in body

    code = asyncio.run(_run("csv"))
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("task_type")
