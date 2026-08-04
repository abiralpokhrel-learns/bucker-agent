"""Replay engine tests (step 23).

Pure tests cover result shape. Database tests skip when Postgres is unavailable.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from bucker.core.events import EventType
from bucker.replay.engine import (
    ReplayError,
    ReplayResult,
    replay_task,
    replay_workspace_for,
    workspace_for,
)

#: True when asyncpg CAN be imported. Postgres may still not be running.
try:
    import asyncpg  # noqa: F401

    _DB_IMPORT_OK = True
except ImportError:
    _DB_IMPORT_OK = False


async def _postgres_reachable() -> bool:
    """Check if Postgres is actually running."""
    if not _DB_IMPORT_OK:
        return False
    from bucker.config import settings
    try:
        pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1,
            max_size=1,
            timeout=3,
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
    """Skip when Postgres is unreachable."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if not await _ensure_db_checked():
            pytest.skip("Postgres not available")
        return await fn(*args, **kwargs)

    return wrapper


# ------------------------------------------------------------- result shape --


def test_replay_result_serializable():
    result = ReplayResult(
        task_id=uuid4(),
        match=True,
        original_passed=True,
        replayed_passed=True,
        original_events=42,
        diagnostics="consistent",
    )
    data = {
        "task_id": str(result.task_id),
        "match": result.match,
        "original_passed": result.original_passed,
        "replayed_passed": result.replayed_passed,
        "original_events": result.original_events,
        "diagnostics": result.diagnostics,
    }
    json.dumps(data)


def test_replay_result_match_false_on_mismatch():
    result = ReplayResult(
        task_id=uuid4(),
        match=False,
        original_passed=True,
        replayed_passed=False,
        original_events=10,
        diagnostics="mismatch",
    )
    assert not result.match
    assert not result.ok


def test_replay_result_ok_is_match_alias():
    result = ReplayResult(
        task_id=uuid4(),
        match=True,
        original_passed=False,
        replayed_passed=False,
    )
    assert result.ok is result.match


# -------------------------------------------------- workspace isolation -----


def test_replay_workspace_is_separate_from_original():
    """Replay must never run against the durable original workspace."""
    tid = uuid4()
    original = workspace_for(str(tid))
    replay = replay_workspace_for(tid)
    assert replay != original
    # The real safety property, modelled exactly as production enforces it:
    # the replay copy lives INSIDE the replay root (same containment rule
    # the sandbox uses — is_relative_to, not a string prefix).
    assert replay.is_relative_to(workspace_for("replay"))
    assert not original.exists()  # creating a replay copy creates nothing durable


def test_replay_workspace_clears_stale_copies():
    """A crashed replay's leftover copy must never be reused."""
    tid = uuid4()
    replay = replay_workspace_for(tid)
    replay.mkdir(parents=True)
    (replay / "stale-marker").write_text("from a crashed replay", encoding="utf-8")

    fresh = replay_workspace_for(tid)
    assert fresh == replay
    assert not (fresh / "stale-marker").exists()  # wiped, not reused


async def test_replay_sandbox_never_mutates_the_original_workspace():
    """The full replay path — sandbox + diff apply — must leave the durable
    original byte-identical. This is the property the isolated-copy design
    exists to guarantee; it is docker-gated like the other container tests.
    """
    from bucker.sandbox.runtime import DockerSandbox, docker_available

    if not await docker_available():
        pytest.skip("docker not available")

    import shutil

    from bucker.replay.engine import replay_workspace_for

    tid = uuid4()
    original = workspace_for(str(tid))
    original.mkdir(parents=True, exist_ok=True)
    (original / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    try:
        replay = replay_workspace_for(tid)
        shutil.copytree(original, replay)

        sandbox = DockerSandbox(replay)
        await sandbox.start()
        try:
            # What replay actually does on the workspace: apply a diff and
            # run something — both write files.
            diff = (
                "--- a/calc.py\n+++ b/calc.py\n"
                "@@ -1,2 +1,4 @@\n def add(a, b):\n     return a + b\n"
                "+def subtract(a, b):\n+    return a - b\n"
            )
            applied = await sandbox.apply_diff(diff, files=["calc.py"])
            assert applied.exit_code == 0, applied.stderr
            await sandbox.exec("python -c 'import py_compile; py_compile.compile(\"calc.py\")'")
        finally:
            await sandbox.stop()

        # The original is the evidence: it must be exactly as seeded.
        assert (original / "calc.py").read_text(encoding="utf-8") == (
            "def add(a, b):\n    return a + b\n"
        )
        # The replay copy carries the mutation (diff applied there).
        assert "subtract" in (replay / "calc.py").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(original, ignore_errors=True)
        shutil.rmtree(replay_workspace_for(tid), ignore_errors=True)


# ---------------------------------------------------------- error handling ---


@requires_db
async def test_empty_event_stream_raises():
    from bucker.config import settings
    from bucker.core.blob import BlobStore
    from bucker.core.eventstore import EventStore, create_pool

    pool = await create_pool(settings.database_url)
    store = EventStore(pool)
    blobs = BlobStore(settings.blob_root)

    try:
        with pytest.raises(ReplayError, match="no events"):
            await replay_task(uuid4(), store=store, blobs=blobs)
    finally:
        await pool.close()


@requires_db
async def test_no_task_created_event_raises():
    from bucker.config import settings
    from bucker.core.blob import BlobStore
    from bucker.core.eventstore import EventStore, create_pool

    pool = await create_pool(settings.database_url)
    store = EventStore(pool)
    blobs = BlobStore(settings.blob_root)
    tid = uuid4()

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, task_type, objective, status) "
                "VALUES ($1, 'demo', 'test', 'pending')",
                tid,
            )
        await store.append(
            tid, EventType.TASK_STARTED, {"reason": "test"},
            idempotency_key=f"{tid}:started",
        )
        with pytest.raises(ReplayError, match="no TaskCreated"):
            await replay_task(tid, store=store, blobs=blobs)
    finally:
        await pool.close()


@requires_db
async def test_task_created_without_objective_raises():
    from bucker.config import settings
    from bucker.core.blob import BlobStore
    from bucker.core.eventstore import EventStore, create_pool

    pool = await create_pool(settings.database_url)
    store = EventStore(pool)
    blobs = BlobStore(settings.blob_root)
    tid = uuid4()

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, task_type, objective, status) "
                "VALUES ($1, 'demo', 'test', 'pending')",
                tid,
            )
        await store.append(
            tid, EventType.TASK_CREATED, {"task_type": "demo"},
            idempotency_key=f"{tid}:created",
        )
        with pytest.raises(ReplayError, match="no objective"):
            await replay_task(tid, store=store, blobs=blobs)
    finally:
        await pool.close()


@requires_db
async def test_no_verification_event_raises():
    from bucker.config import settings
    from bucker.core.blob import BlobStore
    from bucker.core.eventstore import EventStore, create_pool

    pool = await create_pool(settings.database_url)
    store = EventStore(pool)
    blobs = BlobStore(settings.blob_root)
    tid = uuid4()

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (id, task_type, objective, status) "
                "VALUES ($1, 'demo', 'test', 'pending')",
                tid,
            )
        await store.append(
            tid, EventType.TASK_CREATED,
            {"objective": "do something", "task_type": "demo"},
            idempotency_key=f"{tid}:created",
        )
        await store.append(
            tid, EventType.TASK_STARTED, {},
            idempotency_key=f"{tid}:started",
        )
        with pytest.raises(ReplayError, match="no verification event"):
            await replay_task(tid, store=store, blobs=blobs)
    finally:
        await pool.close()
