"""Event store tests (steps 6, 11). Requires Postgres.

    docker compose up -d
    BUCKER_TEST_DATABASE_URL=postgresql://postgres:dev@localhost:5432/bucker uv run pytest

The append-only test at the bottom is the one that matters most: it proves the
immutability claim is enforced by the database, not by good intentions.
"""

from __future__ import annotations

import asyncio

import pytest

from bucker.core.events import EventType
from bucker.core.eventstore import EventStore
from bucker.core.snapshots import SnapshotStore
from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_append_and_read_round_trip(pool, seeded_task):
    store = EventStore(pool)
    await store.append(seeded_task, EventType.TASK_CREATED, {"objective": "test"})
    await store.append(seeded_task, EventType.TASK_STARTED, {})

    events = await store.read_stream(seeded_task)
    assert [e.event_type for e in events] == ["TaskCreated", "TaskStarted"]
    assert events[0].payload == {"objective": "test"}
    assert events[0].schema_version == 1


async def test_stream_is_ordered_by_id(pool, seeded_task):
    store = EventStore(pool)
    for i in range(25):
        await store.append(seeded_task, EventType.STEP_COMPLETED, {"step": f"s{i}"})

    events = await store.read_stream(seeded_task)
    assert [e.id for e in events] == sorted(e.id for e in events)
    assert [e.payload["step"] for e in events] == [f"s{i}" for i in range(25)]


async def test_idempotent_append_returns_original(pool, seeded_task):
    """The exactly-once property behind crash-safe activity retries."""
    store = EventStore(pool)
    first = await store.append(
        seeded_task, EventType.STEP_COMPLETED, {"step": "a"},
        idempotency_key="task:step-a",
    )
    second = await store.append(
        seeded_task, EventType.STEP_COMPLETED, {"step": "a"},
        idempotency_key="task:step-a",
    )

    assert first.id == second.id
    assert await store.count(seeded_task) == 1


async def test_concurrent_appends_with_same_key_insert_once(pool, seeded_task):
    """Two workers racing on the same step must not both win."""
    store = EventStore(pool)
    results = await asyncio.gather(*[
        store.append(seeded_task, EventType.STEP_COMPLETED, {"step": "race"},
                     idempotency_key="task:race")
        for _ in range(10)
    ])
    assert len({e.id for e in results}) == 1
    assert await store.count(seeded_task) == 1


async def test_appends_without_key_are_not_deduped(pool, seeded_task):
    store = EventStore(pool)
    a = await store.append(seeded_task, EventType.STEP_COMPLETED, {"step": "x"})
    b = await store.append(seeded_task, EventType.STEP_COMPLETED, {"step": "x"})
    assert a.id != b.id


async def test_read_after_id_returns_tail(pool, seeded_task):
    store = EventStore(pool)
    ids = []
    for i in range(6):
        e = await store.append(seeded_task, EventType.STEP_COMPLETED, {"step": str(i)})
        ids.append(e.id)

    tail = await store.read_stream(seeded_task, after_id=ids[2])
    assert [e.id for e in tail] == ids[3:]


async def test_tool_output_ref_persists(pool, seeded_task):
    store = EventStore(pool)
    ref = "sha256:" + "a" * 64
    await store.append(seeded_task, EventType.TOOL_CALL_COMPLETED, {},
                       tool_output_ref=ref)
    assert (await store.read_stream(seeded_task))[0].tool_output_ref == ref


async def test_events_table_is_append_only_for_app_role(pool, seeded_task):
    """The immutability guarantee, enforced by Postgres permissions.

    If this test starts passing UPDATEs, someone widened the grant and the
    whole 'event log is the truth' claim quietly became false.
    """
    import asyncpg

    store = EventStore(pool)
    await store.append(seeded_task, EventType.TASK_CREATED, {"objective": "immutable"})

    async with pool.acquire() as conn:
        role = await conn.fetchval("SELECT current_user")
        if role != "bucker_app":
            pytest.skip(f"connected as {role}; run this suite as bucker_app to check grants")

        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("UPDATE events SET payload = '{}'::jsonb")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM events")


async def test_snapshot_store_matches_full_replay(pool, seeded_task):
    store = EventStore(pool)
    snaps = SnapshotStore(pool, store)

    await store.append(seeded_task, EventType.TASK_CREATED, {"objective": "snap test"})
    for i in range(60):
        await store.append(seeded_task, EventType.STEP_COMPLETED, {"step": f"s{i}"})

    via_snapshot = await snaps.get_state(seeded_task)
    full = await snaps.rebuild_full(seeded_task)
    assert via_snapshot == full

    # A snapshot should have been written past the interval.
    assert await snaps.latest(seeded_task) is not None

    # And reading again through the snapshot path still matches.
    assert await snaps.get_state(seeded_task) == full
