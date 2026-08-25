"""Wave-2 CLI feature tests: formats, sweep, forecast, version.

All hermetic — SQLite storage, no Temporal, no network. Each command
test swaps settings.database_url for its own tmp DSN and restores it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from bucker.core.eventstore import EventStore, create_pool


def run_with_dsn(dsn, coro_fn):
    """Run an async command function with settings.database_url swapped."""
    import bucker.config as cfg

    original = cfg.settings.database_url

    async def _run():
        object.__setattr__(cfg.settings, "database_url", dsn)
        try:
            return await coro_fn()
        finally:
            object.__setattr__(cfg.settings, "database_url", original)

    return asyncio.run(_run())


@pytest.fixture
def seeded_db(tmp_path):
    """A sqlite store holding one stale running task and one completed."""

    async def _seed():
        dsn = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
        pool = await create_pool(dsn)
        store = EventStore(pool)
        stale = uuid4()
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        await pool.execute(
            "INSERT INTO tasks (id, task_type, objective, status, "
            "created_at) VALUES ($1, 'code_change', 'stale objective', "
            "'running', $2)",
            stale, old,
        )
        done = uuid4()
        event = await store.append(
            done, "ModelCallCompleted", {"purpose": "worker"},
            idempotency_key=f"{done}:call",
        )
        await pool.execute(
            "INSERT INTO tasks (id, task_type, objective, status) "
            "VALUES ($1, 'demo', 'finished demo', 'completed')",
            done,
        )
        await pool.execute(
            "INSERT INTO telemetry (event_id, task_id, model_used, purpose,"
            " cost_usd, total_tokens) VALUES ($1, $2, 'm/x', 'worker', "
            "0.05, 500)",
            event.id, done,
        )
        return dsn, str(stale), str(done)

    return asyncio.run(_seed())


# ------------------------------------------------------------------ tasks --


def test_tasks_format_json(seeded_db, capsys):
    from bucker.cli import cmd_tasks

    dsn, _, _ = seeded_db
    code = run_with_dsn(
        dsn,
        lambda: cmd_tasks(argparse.Namespace(limit=10, status=None,
                                             format="json")),
    )
    body = json.loads(capsys.readouterr().out)
    assert code == 0
    assert len(body) == 2
    statuses = {t["status"] for t in body}
    assert {"running", "completed"} == statuses


def test_tasks_format_csv(seeded_db, capsys):
    from bucker.cli import cmd_tasks

    dsn, _, _ = seeded_db
    code = run_with_dsn(
        dsn,
        lambda: cmd_tasks(argparse.Namespace(limit=10, status="running",
                                             format="csv")),
    )
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert code == 0
    assert lines[0].startswith("task_id")
    assert len(lines) == 2  # header + the one running task


def test_tasks_status_filter_applies_before_format(seeded_db, capsys):
    from bucker.cli import cmd_tasks

    dsn, _, _ = seeded_db
    code = run_with_dsn(
        dsn,
        lambda: cmd_tasks(argparse.Namespace(limit=10, status="halted",
                                             format="json")),
    )
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip() == "no tasks yet"


# ------------------------------------------------------------------ usage --


def test_usage_format_json_includes_totals(seeded_db, capsys):
    from bucker.cli import cmd_usage

    dsn, _, _ = seeded_db
    code = run_with_dsn(
        dsn, lambda: cmd_usage(argparse.Namespace(format="json")),
    )
    body = json.loads(capsys.readouterr().out)
    assert code == 0
    assert body["total"]["calls"] == 1
    assert body["total"]["cost_usd"] == pytest.approx(0.05)
    assert body["by_model"][0]["model"] == "m/x"
    assert body["by_stage"][0]["purpose"] == "worker"


def test_usage_table_still_renders(seeded_db, capsys):
    from bucker.cli import cmd_usage

    dsn, _, _ = seeded_db
    code = run_with_dsn(
        dsn, lambda: cmd_usage(argparse.Namespace(format="table")),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "by model:" in out and "m/x" in out


# ------------------------------------------------------------------ sweep --


def test_sweep_reports_and_exit_code_one_when_actionable(seeded_db, capsys):
    from bucker.cli import cmd_sweep

    dsn, stale_id, _ = seeded_db
    code = run_with_dsn(
        dsn,
        lambda: cmd_sweep(argparse.Namespace(
            stale_minutes=30, near_budget=0.8, halt=False, notify=False,
            format="table",
        )),
    )
    out = capsys.readouterr().out
    assert code == 1                      # actionable + not halted -> alert
    assert stale_id[:8] in out
    assert "stale" in out.lower()


def test_sweep_halt_resolves_and_exits_zero(seeded_db, capsys):
    from bucker.cli import cmd_sweep

    dsn, stale_id, _ = seeded_db
    code = run_with_dsn(
        dsn,
        lambda: cmd_sweep(argparse.Namespace(
            stale_minutes=30, near_budget=0.8, halt=True, notify=False,
            format="table",
        )),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "halted now (1)" in out

    async def _check():
        pool = await create_pool(dsn)
        try:
            import uuid as _uuid

            return await pool.fetchval(
                "SELECT status FROM tasks WHERE id = $1",
                _uuid.UUID(stale_id),
            )
        finally:
            await pool.close()

    assert asyncio.run(_check()) == "failed"


# ---------------------------------------------------------------- version --


def test_version_prints_version_and_mode(capsys):
    from bucker.cli import cmd_version

    code = asyncio.run(cmd_version(argparse.Namespace()))
    out = capsys.readouterr().out
    assert code == 0
    assert "bucker-agent" in out
    assert "mode" in out


# ---------------------------------------------------------------- replay ----


def test_replay_requires_task_or_recent(capsys):
    """Neither a task id nor --recent is a usage error, not a crash."""
    import bucker.config as cfg
    from bucker.cli import cmd_replay

    original = cfg.settings.database_url

    async def _run():
        # A DSN that never gets touched: the guard fires before any I/O.
        object.__setattr__(cfg.settings, "database_url", "sqlite:///:memory:")
        try:
            return await cmd_replay(argparse.Namespace(
                task_id=None, recent=None, status="completed"))
        finally:
            object.__setattr__(cfg.settings, "database_url", original)

    code = asyncio.run(_run())
    err = capsys.readouterr()
    assert code == 2
    assert "--recent" in err.err