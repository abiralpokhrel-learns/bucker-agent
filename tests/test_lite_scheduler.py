"""Lite-mode schedule tests: cron-backed recurring tasks without Temporal.

Hermetic by construction: SQLite storage (LitePool), the demo pipeline
(no model), and a manually-advanced clock (backdate next_run_at instead of
sleeping). Covers the store CRUD, the POSIX-correct fire loop, claim-
before-run semantics (missed, never duplicated), pause/resume, and the
HTTP surface replacing the old 501s.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

import pytest

from bucker.core.eventstore import EventStore, create_pool
from bucker.core.state import rebuild_state


@pytest.fixture
def lite_db(tmp_path):
    """A fresh sqlite DSN factory with schema applied on each call."""
    def _dsn(name="lite.db"):
        return f"sqlite:///{(tmp_path / name).as_posix()}"
    return _dsn


@pytest.fixture(autouse=True)
def _isolated_blob_root(tmp_path):
    """Demo steps write markers under blob_root/../workspace â€” keep them
    out of the repo tree."""
    import bucker.config as cfg

    original = cfg.settings.blob_root
    object.__setattr__(cfg.settings, "blob_root", tmp_path / "blobs")
    yield
    object.__setattr__(cfg.settings, "blob_root", original)


@pytest.fixture
def patched_demo_store(monkeypatch):
    """Hand the activities the test's EventStore (they otherwise lazily
    build their own from settings.database_url)."""
    def _apply(store):
        import bucker.activities.demo as demo_mod

        monkeypatch.setattr(demo_mod, "_store", store)
    return _apply


async def _wait_for_status(store: EventStore, tid: str,
                           wanted="completed", timeout_s: float = 15.0):
    deadline = asyncio.get_running_loop().time() + timeout_s
    state = {}
    while asyncio.get_running_loop().time() < deadline:
        state = rebuild_state(await store.read_stream(tid))
        if state.get("status") == wanted:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"task {tid} never reached {wanted!r}; last state keys: "
        f"{sorted(state)}"
    )


def _backdate(pool, schedule_id: str, minutes_ago: float = 1.0):
    past = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    return pool.execute(
        "UPDATE schedules SET next_run_at = $2 WHERE id = $1",
        schedule_id, past,
    )


# --------------------------------------------------------------- CRUD ----


async def test_create_and_get_round_trip(lite_db):
    from bucker.lite.scheduler import (
        create_schedule,
        delete_schedule,
        get_schedule,
        list_schedules,
    )

    pool = await create_pool(lite_db())
    try:
        result = await create_schedule(pool, {
            "schedule_id": "nightly-demo",
            "cron": "0 9 * * *",
            "template": "demo",
        })
        assert result["created"] is True
        assert result["objective"].startswith("run the five-step")

        row = await get_schedule(pool, "nightly-demo")
        assert row is not None
        assert row["paused"] is False
        assert row["cron"] == "0 9 * * *"
        assert row["next_run_at"] is not None
        assert row["run_count"] == 0

        listed = await list_schedules(pool)
        assert [s["schedule_id"] for s in listed] == ["nightly-demo"]

        assert await delete_schedule(pool, "nightly-demo") is True
        assert await delete_schedule(pool, "nightly-demo") is False
        assert await get_schedule(pool, "nightly-demo") is None
    finally:
        await pool.close()


async def test_create_upserts_same_id(lite_db):
    """Re-creating an id UPDATES (the re-runnable contract the Temporal
    path offers) rather than erroring or duplicating."""
    from bucker.lite.scheduler import create_schedule, list_schedules

    pool = await create_pool(lite_db())
    try:
        await create_schedule(pool, {
            "schedule_id": "s1", "cron": "0 9 * * *", "template": "demo",
        })
        updated = await create_schedule(pool, {
            "schedule_id": "s1", "cron": "*/10 * * * *", "template": "demo",
        })
        assert updated["cron"] == "*/10 * * * *"
        rows = await list_schedules(pool)
        assert len(rows) == 1
        assert rows[0]["cron"] == "*/10 * * * *"
    finally:
        await pool.close()


async def test_create_rejects_bad_cron(lite_db):
    from bucker.lite.scheduler import create_schedule

    pool = await create_pool(lite_db())
    try:
        with pytest.raises(ValueError, match="[Ii]nvalid cron"):
            await create_schedule(pool, {
                "schedule_id": "bad", "cron": "61 * * * *",
                "template": "demo",
            })
    finally:
        await pool.close()


async def test_create_resolves_objective_override(lite_db):
    from bucker.lite.scheduler import create_schedule, get_schedule

    pool = await create_pool(lite_db())
    try:
        await create_schedule(pool, {
            "schedule_id": "ovr", "cron": "0 * * * *", "template": "demo",
            "objective": "custom objective here",
        })
        row = await get_schedule(pool, "ovr")
        assert row["objective"] == "custom objective here"
    finally:
        await pool.close()


async def test_pause_resume_recomputes_next_run(lite_db):
    """Resuming recomputes next_run_at from NOW: a schedule paused across
    its fire time must not fire immediately (or N times)."""
    from bucker.lite.scheduler import (
        create_schedule,
        get_schedule,
        set_paused,
    )

    pool = await create_pool(lite_db())
    try:
        await create_schedule(pool, {
            "schedule_id": "p", "cron": "* * * * *", "template": "demo",
        })
        before = (await get_schedule(pool, "p"))["next_run_at"]

        paused = await set_paused(pool, "p", paused=True)
        assert paused["paused"] is True
        # Pause keeps the stored next_run_at; the paused flag gates firing.

        resumed = await set_paused(pool, "p", paused=False)
        assert resumed["paused"] is False
        assert resumed["next_run_at"] is not None
        assert resumed["next_run_at"] >= before
    finally:
        await pool.close()


async def test_set_paused_unknown_id_returns_none(lite_db):
    from bucker.lite.scheduler import set_paused

    pool = await create_pool(lite_db())
    try:
        assert await set_paused(pool, "ghost", paused=True) is None
    finally:
        await pool.close()


# -------------------------------------------------------------- firing ----


async def test_tick_fires_due_demo_task_end_to_end(lite_db, patched_demo_store):
    """One tick turns a due schedule row into a real completed task through
    the same create_task path as manual runs."""
    from bucker.lite.scheduler import LiteScheduler, create_schedule, get_schedule

    pool = await create_pool(lite_db())
    store = EventStore(pool)
    patched_demo_store(store)
    try:
        await create_schedule(pool, {
            "schedule_id": "t1", "cron": "* * * * *", "template": "demo",
        })
        await _backdate(pool, "t1")

        scheduler = LiteScheduler(pool)
        fired = await scheduler.tick()
        assert fired == 1

        row = await get_schedule(pool, "t1")
        assert row["run_count"] == 1
        assert row["last_task_id"] is not None
        # next_run_at moved into the future again
        next_dt = datetime.fromisoformat(row["next_run_at"])
        assert next_dt > datetime.now(UTC)

        # The minted task ran the full demo pipeline in-process.
        state = await _wait_for_status(store, row["last_task_id"])
        assert state["status"] == "completed"
    finally:
        await pool.close()
        # Give any still-running spawned runner a beat to finish writing.
        await asyncio.sleep(0.2)


async def test_second_tick_does_not_duplicate(lite_db, patched_demo_store):
    """Claim-before-run: after firing, next_run_at is already in the
    future, so an immediate re-tick fires nothing. Missed runs are
    preferred over duplicated paid pipelines."""
    from bucker.lite.scheduler import LiteScheduler, create_schedule

    pool = await create_pool(lite_db())
    store = EventStore(pool)
    patched_demo_store(store)
    try:
        await create_schedule(pool, {
            "schedule_id": "once", "cron": "* * * * *", "template": "demo",
        })
        await _backdate(pool, "once")

        scheduler = LiteScheduler(pool)
        assert await scheduler.tick() == 1
        assert await scheduler.tick() == 0
    finally:
        await pool.close()
        await asyncio.sleep(0.2)


async def test_paused_schedule_never_fires(lite_db):
    from bucker.lite.scheduler import LiteScheduler, create_schedule, set_paused

    pool = await create_pool(lite_db())
    try:
        await create_schedule(pool, {
            "schedule_id": "quiet", "cron": "* * * * *", "template": "demo",
        })
        await set_paused(pool, "quiet", paused=True)
        await _backdate(pool, "quiet")

        scheduler = LiteScheduler(pool)
        assert await scheduler.tick() == 0
    finally:
        await pool.close()


async def test_tick_fires_multiple_due_schedules(lite_db, patched_demo_store):
    from bucker.lite.scheduler import LiteScheduler, create_schedule

    pool = await create_pool(lite_db())
    store = EventStore(pool)
    patched_demo_store(store)
    try:
        for i in range(3):
            await create_schedule(pool, {
                "schedule_id": f"m{i}", "cron": "* * * * *",
                "template": "demo",
            })
            await _backdate(pool, f"m{i}", minutes_ago=float(i + 1))

        scheduler = LiteScheduler(pool)
        assert await scheduler.tick() == 3
    finally:
        await pool.close()
        await asyncio.sleep(0.4)


async def test_scheduler_loop_survives_store_errors(lite_db, monkeypatch):
    """A failing tick must not kill the loop (the process IS the scheduler
    in lite mode)."""
    from bucker.lite.scheduler import LiteScheduler

    pool = await create_pool(lite_db())
    try:
        scheduler = LiteScheduler(pool)

        async def _boom():
            raise RuntimeError("simulated db outage")

        monkeypatch.setattr(scheduler, "tick", _boom)
        await scheduler.start()
        await asyncio.sleep(0.3)
        assert not scheduler._loop_task.done()
        await scheduler.stop()
        assert scheduler._loop_task is None
    finally:
        await pool.close()



@pytest.fixture
def lite_api(tmp_path):
    """TestClient over the app module with a LitePool injected.

    No lifespan runs (the pool arrives pre-injected), so the scheduler
    loop stays off. App globals are restored afterwards so other test
    modules are unaffected.
    """

    from fastapi.testclient import TestClient

    import bucker.api  # noqa: F401 — ensures the app module is imported

    # bucker/api/__init__ shadows the name `app` with the FastAPI
    # instance; the MODULE only reachable via sys.modules.
    app_mod = sys.modules["bucker.api.app"]

    dsn = f"sqlite:///{(tmp_path / 'http.db').as_posix()}"

    async def _make():
        return await create_pool(dsn)

    pool = asyncio.run(_make())
    saved = (app_mod._pool, app_mod._store)
    app_mod._pool = pool
    app_mod._store = EventStore(pool)
    try:
        yield TestClient(app_mod.app, raise_server_exceptions=False)
    finally:
        app_mod._pool, app_mod._store = saved
        asyncio.run(pool.close())


def test_http_schedules_crud_in_lite(lite_api):
    """The old 501 seam now serves full schedule CRUD backed by SQLite."""
    c = lite_api

    created = c.post(
        "/schedules",
        params={
            "schedule_id": "api-nightly",
            "cron": "0 9 * * 1-5",
            "template": "demo",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["schedule_id"] == "api-nightly"
    assert body["created"] is True
    assert body["next_run_at"] is not None

    listing = c.get("/schedules")
    assert listing.status_code == 200
    rows = listing.json()["schedules"]
    assert [r["schedule_id"] for r in rows] == ["api-nightly"]

    paused = c.post("/schedules/api-nightly/pause")
    assert paused.status_code == 200
    assert paused.json()["paused"] is True

    resumed = c.post(
        "/schedules/api-nightly/pause", params={"resume": "true"}
    )
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False

    gone = c.delete("/schedules/api-nightly")
    assert gone.status_code == 200
    missing = c.delete("/schedules/api-nightly")
    assert missing.status_code == 404


def test_http_create_rejects_bad_cron_with_422(lite_api):
    resp = lite_api.post(
        "/schedules",
        params={
            "schedule_id": "broken",
            "cron": "not-a-cron",
            "template": "demo",
        },
    )
    assert resp.status_code == 422
    assert "cron" in resp.json()["detail"].lower()


def test_http_create_unknown_template_is_400(lite_api):
    resp = lite_api.post(
        "/schedules",
        params={
            "schedule_id": "notmpl",
            "cron": "0 9 * * *",
            "template": "does-not-exist",
        },
    )
    assert resp.status_code == 400
    assert "unknown template" in resp.json()["detail"].lower()


# ------------------------------------------------------------ CLI wiring --


def test_cli_schedules_list_works_on_sqlite_dsn(tmp_path, capsys):
    """`bucker schedules list` reads the sqlite store directly instead of
    demanding Temporal."""
    import argparse

    import bucker.config as cfg
    from bucker.cli import cmd_schedules_list
    from bucker.lite.scheduler import create_schedule as lite_create

    dsn = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    original_dsn = cfg.settings.database_url

    async def _run():
        object.__setattr__(cfg.settings, "database_url", dsn)
        try:
            pool = await create_pool(dsn)
            try:
                await lite_create(pool, {
                    "schedule_id": "cli-one",
                    "cron": "0 9 * * *",
                    "template": "demo",
                })
            finally:
                await pool.close()
            return await cmd_schedules_list(argparse.Namespace())
        finally:
            object.__setattr__(cfg.settings, "database_url", original_dsn)

    code = asyncio.run(_run())
    captured = capsys.readouterr()
    assert code == 0
    assert "cli-one" in captured.out
    assert "active" in captured.out
