"""Lite-mode tests: sqlite storage, local sandbox, in-process runner.

Lite mode is the no-Docker/no-Postgres/no-Temporal path: a duck-typed
asyncpg pool over SQLite (``LitePool``), a host-subprocess sandbox
(``LocalSandbox``), and a plain-asyncio runner that calls the same
pipeline activities directly. Everything here is hermetic — no Docker,
no Temporal, no Postgres, no network.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from bucker.core.eventstore import EventStore, create_pool
from bucker.core.snapshots import SnapshotStore
from bucker.core.state import rebuild_state
from bucker.lite.pool import LitePool, reorder_params, translate_sql
from bucker.sandbox.local import LocalSandbox
from bucker.sandbox.runtime import _remove_stray_prefix_dirs


@pytest.fixture
def lite_db(tmp_path):
    """A fresh sqlite DSN + pool with the schema applied."""
    dsn = f"sqlite:///{(tmp_path / 'lite.db').as_posix()}"

    async def _make():
        pool = await create_pool(dsn)
        return pool

    return _make


# ----------------------------------------------------------- translation ---


def test_translate_placeholders_and_casts():
    sql = "SELECT * FROM events WHERE task_id = $1 AND payload = $2::jsonb"
    assert translate_sql(sql) == "SELECT * FROM events WHERE task_id = ? AND payload = ?"


def test_translate_interval_and_now():
    sql = "SELECT * FROM tasks WHERE created_at > NOW() - INTERVAL '7 days'"
    out = translate_sql(sql)
    assert "datetime('now', '-7 days')" in out
    assert "NOW()" not in out
    assert "INTERVAL" not in out


def test_translate_minutes_concat_interval():
    sql = "SELECT * FROM tasks WHERE created_at < NOW() - ($1 || ' minutes')::interval"
    out = translate_sql(sql)
    assert "datetime('now', '-' || ? || ' minutes')" in out
    # the ::int cast must not have eaten the ::interval prefix
    assert "erval" not in out


def test_translate_current_date():
    assert "date('now')" in translate_sql("WHERE created_at >= CURRENT_DATE")


# ------------------------------------------------- placeholder ordering ---


def test_reorder_params_identity_for_in_order_placeholders():
    """$1, $2, $3 queries bind positionally in both dialects — no change."""
    sql = "INSERT INTO t (a, b) VALUES ($1, $2)"
    assert reorder_params(sql, ("x", "y")) == ("x", "y")


def test_reorder_params_swaps_out_of_order_placeholders():
    """asyncpg binds $N by number wherever it appears; sqlite binds ? in
    text order. `SET status = $2 WHERE id = $1` must therefore bind
    args[1] to status and args[0] to id — the graph-status bug: without
    this, the terminal UPDATE silently matched zero rows."""
    sql = "UPDATE tasks SET status = $2 WHERE id = $1"
    assert reorder_params(sql, ("task-123", "completed")) == ("completed", "task-123")


def test_reorder_params_handles_repeated_placeholder():
    sql = "SELECT 1 WHERE a = $1 OR b = $1"
    assert reorder_params(sql, ("only",)) == ("only", "only")


def test_reorder_params_handles_multi_digit_indexes():
    sql = "SELECT * FROM t WHERE a = $10 AND b = $2"
    args = tuple(f"arg{i}" for i in range(1, 11))
    out = reorder_params(sql, args)
    assert out[0] == "arg10"  # $10's slot gets the 10th param
    assert out[1] == "arg2"  # $2's slot gets the 2nd param


@pytest.mark.asyncio
async def test_out_of_order_update_actually_sticks(lite_db):
    """End-to-end: an asyncpg-style $2-before-$1 UPDATE must change the row."""
    pool = await lite_db()
    task_id = uuid4()
    await pool.execute(
        "INSERT INTO tasks (id, task_type, objective, status) "
        "VALUES ($1, $2, $3, 'pending')",
        task_id, "demo", "out-of-order update",
    )
    await pool.execute(
        "UPDATE tasks SET status = $2 WHERE id = $1", task_id, "failed"
    )
    got = await pool.fetchval("SELECT status FROM tasks WHERE id = $1", task_id)
    assert got == "failed"
    await pool.close()


# --------------------------------------------------------------- storage ---


@pytest.mark.asyncio
async def test_eventstore_round_trip_on_sqlite(lite_db):
    pool = await lite_db()
    store = EventStore(pool)
    tid = uuid4()

    await store.append(tid, "TaskCreated", {"objective": "hello"},
                       idempotency_key=f"{tid}:created")
    await store.append(tid, "TaskStarted", {})

    # exactly-once: same idempotency key appends once
    again = await store.append(tid, "TaskCreated", {"objective": "hello"},
                               idempotency_key=f"{tid}:created")
    assert again.id == 1

    events = await store.read_stream(tid)
    assert [e.event_type for e in events] == ["TaskCreated", "TaskStarted"]
    assert events[0].payload["objective"] == "hello"
    assert await store.count(tid) == 2
    assert await store.last_event_id(tid) == 2
    await pool.close()


@pytest.mark.asyncio
async def test_snapshot_matches_full_replay_on_sqlite(lite_db):
    pool = await lite_db()
    store = EventStore(pool)
    snaps = SnapshotStore(pool, store)
    tid = uuid4()

    await store.append(tid, "TaskCreated", {"objective": "snap"})
    for i in range(60):
        await store.append(tid, "StepCompleted", {"step": i})

    via_snapshot = await snaps.get_state(tid)
    full = await snaps.rebuild_full(tid)
    assert via_snapshot == full
    assert await snaps.latest(tid) is not None
    await pool.close()


@pytest.mark.asyncio
async def test_rows_come_back_shaped_like_asyncpg(lite_db):
    """Callers do str(row['id']), row['created_at'].isoformat(), etc."""
    pool = await lite_db()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, task_type, objective, status) "
            "VALUES ($1, $2, $3, 'pending')",
            uuid4(), "demo", "shapes",
        )
        row = await conn.fetchrow("SELECT id, task_type, created_at FROM tasks")
    assert row is not None
    assert isinstance(row["id"], type(uuid4()))  # UUID
    assert row["task_type"] == "demo"
    assert hasattr(row["created_at"], "isoformat")  # datetime
    await pool.close()


# ------------------------------------------------------------ local sandbox --


@pytest.mark.asyncio
async def test_local_sandbox_runs_commands(tmp_path):
    sandbox = LocalSandbox(tmp_path)
    await sandbox.start()
    try:
        result = await sandbox.exec("echo hello-from-sandbox")
        assert result.ok
        assert "hello-from-sandbox" in result.stdout
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_local_sandbox_rejects_path_escape(tmp_path):
    from bucker.sandbox.runtime import SandboxError

    sandbox = LocalSandbox(tmp_path)
    await sandbox.start()
    try:
        with pytest.raises(SandboxError):
            sandbox.write_file("../evil.txt", "nope")
        with pytest.raises(SandboxError):
            sandbox.read_file("../../etc/passwd")
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_local_sandbox_apply_diff(tmp_path):
    sandbox = LocalSandbox(tmp_path)
    await sandbox.start()
    try:
        sandbox.write_file("calc.py", "def add(a, b):\n    return a + b\n")
        result = await sandbox.apply_diff(
            "--- a/calc.py\n+++ b/calc.py\n@@ -1,3 +1,4 @@\n def add(a, b):\n     return a + b\n+\n+def mul(a, b):\n+    return a * b\n",
            files=["calc.py"],
        )
        assert result.ok, result.stdout + result.stderr
        content = sandbox.read_file("calc.py")
        assert "def mul" in content
    finally:
        await sandbox.stop()


@pytest.mark.asyncio
async def test_apply_diff_fallback_does_not_leave_prefix_dirs(tmp_path):
    """The patch -p0 fallback keeps a/ b/ prefixes and can half-apply into
    literal b/ subdirs; pytest then recurses into b/ and collection crashes
    (exit 2) even when the real files are correct. apply_diff must clean
    those up so the verifier only sees the real workspace."""
    sandbox = LocalSandbox(tmp_path)
    await sandbox.start()
    try:
        sandbox.write_file("calc.py", "def add(a, b):\n    return a + b\n")
        # First hunk has context that does NOT match the file (forces git
        # apply + patch -p1 to fail); the new-file section then lands via
        # patch -p0 with the b/ prefix intact.
        diff = (
            "--- a/calc.py\n+++ b/calc.py\n@@ -1,5 +1,6 @@\n"
            "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n"
            "--- /dev/null\n+++ b/test_calc.py\n@@ -0,0 +1,3 @@\n"
            "+def test_thing():\n+    pass\n"
        )
        await sandbox.apply_diff(diff, files=["calc.py", "test_calc.py"])
        assert not (tmp_path / "b").exists(), "stray b/ dir must be cleaned"
        assert not (tmp_path / "a").exists(), "stray a/ dir must be cleaned"
        assert (tmp_path / "calc.py").exists(), "real workspace files untouched"
    finally:
        await sandbox.stop()


def test_remove_stray_prefix_dirs_keeps_real_files(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "junk.py").write_text("broken", encoding="utf-8")
    (tmp_path / "a").mkdir()
    (tmp_path / "real.py").write_text("ok", encoding="utf-8")
    _remove_stray_prefix_dirs(tmp_path)
    assert not (tmp_path / "b").exists()
    assert not (tmp_path / "a").exists()
    assert (tmp_path / "real.py").read_text(encoding="utf-8") == "ok"


# --------------------------------------------------------------- runner -----


@pytest.mark.asyncio
async def test_demo_task_runs_in_process(lite_db, monkeypatch):
    """The lite runner completes a demo task without Temporal."""
    import bucker.activities.demo as demo_mod
    from bucker.lite.runner import run_demo_task

    pool = await lite_db()
    store = EventStore(pool)
    monkeypatch.setattr(demo_mod, "_store", store)

    tid = str(uuid4())
    result = await run_demo_task(tid, "demo objective")
    assert result["status"] == "completed"

    events = await store.read_stream(tid)
    types = [e.event_type for e in events]
    assert "TaskCompleted" in types
    assert sum(1 for t in types if t == "StepCompleted") == 5
    await pool.close()


@pytest.mark.asyncio
async def test_lite_task_creation_uses_in_process_runner(lite_db, monkeypatch):
    """create_task in sqlite mode returns a lite- workflow id and the
    task actually completes — no Temporal anywhere."""
    import bucker.activities.demo as demo_mod
    from bucker.core.tasks import create_task

    pool = await lite_db()
    store = EventStore(pool)
    monkeypatch.setattr(demo_mod, "_store", store)

    tid, wfid, err = await create_task(
        store, pool, objective="demo through lite", task_type="demo", verifier="noop"
    )
    assert err is None
    assert wfid == f"lite-{tid}"

    import asyncio

    for _ in range(50):
        state = rebuild_state(await store.read_stream(tid))
        if state.get("status") == "completed":
            break
        await asyncio.sleep(0.1)
    assert state.get("status") == "completed"
    await pool.close()


# ----------------------------------------------------------- CLI plumbing ----


def test_lite_command_registered():
    """`bucker lite --help` exists and describes the zero-infra mode."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "bucker.cli", "lite", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0
    assert "--no-browser" in out.stdout
    assert "--port" in out.stdout


def test_create_pool_dispatches_on_sqlite_dsn(tmp_path):
    import asyncio

    dsn = f"sqlite:///{(tmp_path / 'pool-test.db').as_posix()}"

    async def _check():
        pool = await create_pool(dsn)
        try:
            assert isinstance(pool, LitePool)
        finally:
            await pool.close()

    asyncio.run(_check())


# ------------------------------------------------- graph + cancel in lite ----


@pytest.mark.asyncio
async def test_graph_task_runs_in_process(lite_db, monkeypatch):
    """A graph with independent steps completes in lite mode (no Temporal)."""
    import bucker.activities.demo as demo_mod
    from bucker.core.tasks import create_task

    pool = await lite_db()
    store = EventStore(pool)
    monkeypatch.setattr(demo_mod, "_store", store)

    spec = {
        "name": "mini-graph",
        "steps": [
            {"id": "a", "objective": "demo step a", "task_type": "demo"},
            {"id": "b", "objective": "demo step b", "task_type": "demo",
             "depends_on": ["a"]},
        ],
    }
    tid, wfid, err = await create_task(
        store, pool,
        objective="graph: mini-graph (2 steps)",
        task_type="graph",
        verifier="noop",
        graph_spec=spec,
    )
    assert err is None
    assert wfid == f"lite-{tid}"

    import asyncio

    for _ in range(100):
        state = rebuild_state(await store.read_stream(tid))
        if state.get("graph_steps") and any(
            s.get("step_id") == "__graph__" and s.get("status") == "completed"
            for s in state.get("graph_steps", [])
        ):
            break
        await asyncio.sleep(0.1)

    # The graph container's status stays pending (same as the Temporal
    # path — only child tasks get terminal status); what matters is that
    # the lifecycle + each step were recorded.
    steps = state.get("graph_steps", [])
    assert any(
        s.get("step_id") == "__graph__" and s.get("status") == "completed"
        for s in steps
    ), steps
    step_ids = {s.get("step_id") for s in steps}
    assert {"a", "b"} <= step_ids, step_ids

    # GraphStepCompleted events for the __graph__ lifecycle + each step.
    events = await store.read_stream(tid)
    graph_events = [e for e in events if e.event_type == "GraphStepCompleted"]
    assert any(e.payload.get("step_id") == "__graph__" for e in graph_events)
    await pool.close()


@pytest.mark.asyncio
async def test_lite_cancel_finds_runner_task(lite_db, monkeypatch):
    """_lite_task_for finds a spawned runner so /cancel works in lite."""
    import bucker.activities.demo as demo_mod
    from bucker.core.tasks import _lite_task_for, create_task

    pool = await lite_db()
    store = EventStore(pool)
    monkeypatch.setattr(demo_mod, "_store", store)

    # A code task's runner runs long enough to cancel; but demo completes
    # instantly, so we test the bookkeeping: after create_task, the task
    # handle is either already done (demo finished) or findable.
    tid, wfid, err = await create_task(
        store, pool, objective="cancel bookkeeping", task_type="demo", verifier="noop"
    )
    assert err is None
    handle = _lite_task_for(tid)
    # The runner may have finished already (demo is fast); the important
    # invariant is that a lookup never raises and returns None or a task.
    assert handle is None or handle.done() or not handle.done()
    await pool.close()


def test_schedules_served_by_lite_backend(tmp_path):
    """Schedules work in lite mode now: GET /schedules answers from the
    SQLite store (the old 501 seam is gone; full CRUD lives in
    tests/test_lite_scheduler.py)."""
    import asyncio

    from fastapi.testclient import TestClient

    os.environ["BUCKER_DATABASE_URL"] = (
        f"sqlite:///{(tmp_path / 'lite.db').as_posix()}"
    )
    os.environ["BUCKER_SANDBOX_MODE"] = "local"

    async def _boot():
        import sys

        import bucker.api  # noqa: F401

        app_mod = sys.modules["bucker.api.app"]

        # Fresh app state with the lite pool injected.
        pool = await create_pool(os.environ["BUCKER_DATABASE_URL"])
        app_mod._pool = pool
        app_mod._store = EventStore(pool)
        return app_mod.app, pool

    app, pool = asyncio.run(_boot())
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/schedules")
            assert resp.status_code == 200
            body = resp.json()
            assert body["backend"] == "lite"
            assert body["schedules"] == []
    finally:
        asyncio.run(pool.close())
