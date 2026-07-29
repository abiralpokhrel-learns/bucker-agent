"""Snapshots: bounded-cost state reconstruction.

[HAND] — the correctness bar is one sentence: reconstructing via a snapshot
must produce *exactly* the same state as folding the entire stream. If that
ever drifts, every downstream guarantee (resume, replay, audit) is void.
tests/test_snapshots.py property-tests this over random streams.

Snapshots are a cache, never a source of truth. Deleting every snapshot row
must leave the system fully correct, only slower.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

import asyncpg

from bucker.core.eventstore import EventStore
from bucker.core.state import State, rebuild_state

#: Fold at most this many events on top of a snapshot before writing a new one.
SNAPSHOT_INTERVAL = 50


@dataclass(frozen=True, slots=True)
class Snapshot:
    task_id: UUID
    version: int          # events.id of the last event folded in
    state: State


class SnapshotStore:
    def __init__(self, pool: asyncpg.Pool, events: EventStore) -> None:
        self._pool = pool
        self._events = events

    async def latest(self, task_id: UUID) -> Snapshot | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT task_id, version, state FROM snapshots "
                "WHERE task_id = $1 ORDER BY version DESC LIMIT 1",
                task_id,
            )
        if row is None:
            return None
        state = row["state"]
        if isinstance(state, str):
            state = json.loads(state)
        return Snapshot(task_id=row["task_id"], version=row["version"], state=state)

    async def save(self, task_id: UUID, version: int, state: State) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO snapshots (task_id, version, state)
                VALUES ($1, $2, $3::jsonb)
                ON CONFLICT (task_id, version) DO NOTHING
                """,
                task_id,
                version,
                json.dumps(state),
            )

    # ------------------------------------------------------------- read ----
    async def get_state(self, task_id: UUID, *, auto_snapshot: bool = True) -> State:
        """Current state, using the newest snapshot plus the tail of events."""
        snapshot = await self.latest(task_id)

        if snapshot is None:
            events = await self._events.read_stream(task_id)
            state = rebuild_state(events)
            base_version = 0
        else:
            events = await self._events.read_stream(task_id, after_id=snapshot.version)
            state = rebuild_state(events, base=snapshot.state)
            base_version = snapshot.version

        if auto_snapshot and len(events) >= SNAPSHOT_INTERVAL:
            new_version = state.get("last_event_id", base_version)
            if new_version > base_version:
                await self.save(task_id, new_version, state)

        return state

    async def rebuild_full(self, task_id: UUID) -> State:
        """Fold the entire stream, ignoring snapshots. The reference answer.

        Used by tests and by the replay engine's consistency check — if this
        ever disagrees with ``get_state``, the snapshot layer is broken.
        """
        return rebuild_state(await self._events.read_stream(task_id))
