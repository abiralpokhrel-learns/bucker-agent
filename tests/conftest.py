"""Shared fixtures.

Tests split into two families:

  * pure  — no database, always run (state fold, blobs, contracts). Fast.
  * db    — need Postgres; skipped automatically when BUCKER_TEST_DATABASE_URL
            is unset, so `pytest` works on a laptop with nothing running.

Run the db family:
    docker compose up -d
    BUCKER_TEST_DATABASE_URL=postgresql://postgres:dev@localhost:5432/bucker uv run pytest
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from bucker.core.eventstore import Event

TEST_DSN = os.environ.get("BUCKER_TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not TEST_DSN, reason="set BUCKER_TEST_DATABASE_URL to run database tests"
)


# ------------------------------------------------------------ pure helpers --
@pytest.fixture
def task_id() -> UUID:
    return uuid4()


def make_event(
    event_id: int,
    event_type: str,
    payload: dict | None = None,
    *,
    task_id: UUID | None = None,
    tool_output_ref: str | None = None,
) -> Event:
    """Build an Event without touching a database."""
    return Event(
        id=event_id,
        task_id=task_id or uuid4(),
        event_type=event_type,
        payload=payload or {},
        schema_version=1,
        created_at=datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC),
        tool_output_ref=tool_output_ref,
    )


@pytest.fixture
def blob_root(tmp_path: Path) -> Path:
    return tmp_path / "blobs"


# --------------------------------------------------------------- db fixtures --
@pytest.fixture
async def pool():
    """Migrated, isolated pool. Truncates between tests."""
    import asyncpg

    from bucker.core.eventstore import create_pool

    migrations = Path(__file__).resolve().parent.parent / "migrations"
    admin = await asyncpg.connect(TEST_DSN)
    try:
        for path in sorted(migrations.glob("*.sql")):
            await admin.execute(path.read_text())
        await admin.execute(
            "TRUNCATE telemetry, snapshots, events, candidates, tasks CASCADE"
        )
    finally:
        await admin.close()

    p = await create_pool(TEST_DSN)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
async def seeded_task(pool):
    """A tasks row to hang events off (events.task_id is a real FK)."""
    tid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, task_type, objective, status) "
            "VALUES ($1, 'demo', 'test task', 'pending')",
            tid,
        )
    return tid
