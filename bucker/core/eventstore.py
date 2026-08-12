"""Append-only event store.

[HAND] — this is the foundation. If it is subtly wrong, every guarantee above
it is a lie. Read every line; do not regenerate this file casually.

Contract:
  * ``append`` never mutates an existing row. Ever.
  * ``append`` with an ``idempotency_key`` is exactly-once per (task, key):
    a retried activity that appends again gets the ORIGINAL event back.
  * ``read_stream`` returns events in ``id`` order, which is the only ordering
    the rest of the system may rely on (``created_at`` can tie).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from bucker.core.events import SCHEMA_VERSION, EventType

if TYPE_CHECKING:
    import asyncpg


@dataclass(frozen=True, slots=True)
class Event:
    """One immutable fact. Frozen on purpose — you cannot edit the past."""

    id: int
    task_id: UUID
    event_type: str
    payload: dict[str, Any]
    schema_version: int
    created_at: datetime
    tool_output_ref: str | None = None
    idempotency_key: str | None = None

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Event:
        payload = row["payload"]
        # asyncpg returns jsonb as str unless a codec is registered; handle both
        # so this works regardless of pool setup.
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            event_type=row["event_type"],
            payload=payload or {},
            schema_version=row["schema_version"],
            created_at=row["created_at"],
            tool_output_ref=row["tool_output_ref"],
            idempotency_key=row["idempotency_key"],
        )


_COLUMNS = (
    "id, task_id, event_type, payload, schema_version, "
    "created_at, tool_output_ref, idempotency_key"
)


class EventStore:
    """All reads and writes of the event log go through here. No exceptions."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # ------------------------------------------------------------ write ----
    async def append(
        self,
        task_id: UUID,
        event_type: EventType | str,
        payload: dict[str, Any] | None = None,
        *,
        tool_output_ref: str | None = None,
        idempotency_key: str | None = None,
        schema_version: int = SCHEMA_VERSION,
    ) -> Event:
        """Append one event and return it.

        With ``idempotency_key`` this is exactly-once: calling it twice with the
        same (task_id, key) inserts once and returns the first event both times.
        That property is what makes activity retries safe, so the workflow can
        crash between "side effect done" and "event written" without corrupting
        the stream.
        """
        payload = payload or {}
        event_type = str(event_type)

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO events (
                    task_id, event_type, payload, schema_version,
                    tool_output_ref, idempotency_key
                )
                VALUES ($1, $2, $3::jsonb, $4, $5, $6)
                ON CONFLICT (task_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL
                    DO NOTHING
                RETURNING {_COLUMNS}
                """,
                task_id,
                event_type,
                json.dumps(payload),
                schema_version,
                tool_output_ref,
                idempotency_key,
            )

            if row is not None:
                return Event.from_row(row)

            # ON CONFLICT DO NOTHING returned no row => this exact step was
            # already recorded. Hand back the original; the caller must not be
            # able to tell whether it was the first or the fiftieth attempt.
            existing = await conn.fetchrow(
                f"SELECT {_COLUMNS} FROM events "
                f"WHERE task_id = $1 AND idempotency_key = $2",
                task_id,
                idempotency_key,
            )
            if existing is None:  # pragma: no cover - only on real DB weirdness
                raise RuntimeError(
                    f"append conflicted but no existing event found for "
                    f"task={task_id} key={idempotency_key!r}"
                )
            return Event.from_row(existing)

    # ------------------------------------------------------------- read ----
    async def read_stream(
        self,
        task_id: UUID,
        *,
        after_id: int = 0,
        limit: int | None = None,
    ) -> list[Event]:
        """Return this task's events in append order (``id`` ascending)."""
        sql = (
            f"SELECT {_COLUMNS} FROM events "
            f"WHERE task_id = $1 AND id > $2 ORDER BY id ASC"
        )
        args: list[Any] = [task_id, after_id]
        if limit is not None:
            sql += " LIMIT $3"
            args.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [Event.from_row(r) for r in rows]

    async def last_event_id(self, task_id: UUID) -> int:
        """Highest event id for a task, or 0 if the stream is empty."""
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COALESCE(MAX(id), 0) FROM events WHERE task_id = $1",
                task_id,
            )
        return int(value or 0)

    async def count(self, task_id: UUID) -> int:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COUNT(*) FROM events WHERE task_id = $1", task_id
            )
        return int(value or 0)


# --------------------------------------------------------------- pooling ----
async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 10):
    """Create a pool with a jsonb codec so payloads come back as dicts.

    ``sqlite:`` DSNs get a LitePool instead — the no-Docker/no-Postgres
    storage backend (see bucker/lite/pool.py). The caller sees the same
    surface (acquire/fetchrow/fetchval/execute) either way.
    """
    if dsn.startswith("sqlite:"):
        from bucker.lite.pool import LitePool, sqlite_url_to_path

        pool = LitePool(sqlite_url_to_path(dsn))
        await pool.init_schema()
        return pool

    import asyncpg  # postgres backend only (bucker[full])

    async def _init(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    return await asyncpg.create_pool(
        dsn, min_size=min_size, max_size=max_size, init=_init
    )
