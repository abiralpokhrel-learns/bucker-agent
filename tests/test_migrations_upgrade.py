"""Migration upgrade test against a REAL Postgres (hardening review #7).

Applies the migrations in order onto a scratch schema and asserts the
safety properties the release checklist depends on:
  1. 001 alone REJECTS the human-review statuses (the bug the review
     found — the CHECK constraint predates the feature);
  2. applying 001->002->003 makes them ACCEPTED;
  3. re-applying everything is idempotent (bucker migrate re-runs all
     files top-to-bottom, so every migration must be safe to repeat).

DB-gated like the other database tests: skipped unless
BUCKER_TEST_DATABASE_URL is set.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import asyncpg
import pytest

pytestmark = pytest.mark.skipif(
    not __import__("os").environ.get("BUCKER_TEST_DATABASE_URL"),
    reason="set BUCKER_TEST_DATABASE_URL to run migration tests",
)


def _run(coro):
    # One persistent loop per test (set by the fixture) — asyncpg
    # connections cannot be moved between event loops.
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def scratch_schema():
    """A throwaway schema, dropped afterwards (never touches real tables)."""
    import os

    dsn = os.environ["BUCKER_TEST_DATABASE_URL"]
    name = f"migration_test_{uuid.uuid4().hex[:10]}"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    conn = None
    try:
        conn = _run(asyncpg.connect(dsn))
        _run(conn.execute(f'CREATE SCHEMA "{name}"'))
        _run(conn.execute(f'SET search_path TO "{name}"'))
        yield conn
    finally:
        if conn is not None:
            _run(conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
            _run(conn.close())
        loop.close()


def _apply(conn, *names: str) -> None:
    for name in names:
        sql = (Path(__file__).resolve().parent.parent / "migrations" / name) \
            .read_text(encoding="utf-8")
        _run(conn.execute(sql))


def _try_status(conn, status: str) -> bool:
    """True when the constraint ACCEPTS the status."""
    try:
        _run(conn.execute(
            "INSERT INTO tasks (task_type, status, objective) "
            "VALUES ('code_change', $1, 'x')", status))
        return True
    except asyncpg.exceptions.CheckViolationError:
        return False


def test_001_rejects_human_review_statuses(scratch_schema):
    """The schema BEFORE migration 003 must not silently accept the gate's
    statuses — this is the exact regression the review caught."""
    _apply(scratch_schema, "001_init.sql")
    assert _try_status(scratch_schema, "human_approved") is False
    assert _try_status(scratch_schema, "human_rejected") is False


def test_full_upgrade_accepts_human_review_statuses(scratch_schema):
    _apply(scratch_schema, "001_init.sql", "002_telemetry_tokens.sql",
           "003_human_review_statuses.sql")
    assert _try_status(scratch_schema, "human_approved") is True
    assert _try_status(scratch_schema, "human_rejected") is True
    assert _try_status(scratch_schema, "bogus") is False  # guard still on


def test_migrations_are_idempotent(scratch_schema):
    """bucker migrate re-runs every file on each invocation."""
    _apply(scratch_schema, "001_init.sql")
    _apply(scratch_schema, "001_init.sql")
    _apply(scratch_schema, "002_telemetry_tokens.sql",
           "003_human_review_statuses.sql")
    _apply(scratch_schema, "001_init.sql", "002_telemetry_tokens.sql",
           "003_human_review_statuses.sql")
    # Still functional after the double-apply.
    assert _try_status(scratch_schema, "human_approved") is True
