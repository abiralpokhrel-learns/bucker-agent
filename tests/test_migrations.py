"""Migration tripwires (review-driven): schema must match what code writes.

If a migration ever drops the human-review statuses (or the original
constraint is re-added without them), the approval gate silently breaks
on a real database — the API tests use a fake connection that accepts
any UPDATE, so only this tripwire catches it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import requires_db

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS = _ROOT / "migrations"


def test_human_review_statuses_are_legal_in_schema():
    m001 = (_MIGRATIONS / "001_init.sql").read_text(encoding="utf-8")
    m003 = (_MIGRATIONS / "003_human_review_statuses.sql").read_text(encoding="utf-8")

    assert "tasks_status_check" in m003  # 003 re-declares the constraint
    assert "human_approved" in m003
    assert "human_rejected" in m003
    # The original constraint must NOT be the final word on statuses.
    assert "human_approved" not in m001


def test_migration_003_is_idempotent():
    """Migrations re-run top-to-bottom on every `bucker migrate`."""
    m003 = (_MIGRATIONS / "003_human_review_statuses.sql").read_text(encoding="utf-8")
    assert "DROP CONSTRAINT IF EXISTS" in m003  # safe on repeat


def test_schedule_failed_status_is_legal_in_schema():
    """The scheduler writes 'schedule_failed' (api/app.py, core/tasks.py);
    the constraint must allow it. CI exposed this: the original constraint
    (001) and the human-review relaxation (003) did not include it, so the
    write violated the CHECK on a real database."""
    m005 = (_MIGRATIONS / "005_schedule_failed_status.sql").read_text(encoding="utf-8")
    assert "tasks_status_check" in m005
    assert "schedule_failed" in m005
    # 005 must be the FINAL word on statuses (it re-declares the check after
    # 001 and 003, and the DB test below proves the composed constraint).


@requires_db
async def test_schedule_failed_status_inserts_on_real_db(pool):
    """005 must make 'schedule_failed' a legal status on a real database —
    the static tripwire above is file-content; this proves the composed
    constraint accepts it (and still rejects unknown statuses)."""
    import uuid

    import asyncpg

    tid = uuid.uuid4()
    await pool.execute(
        "INSERT INTO tasks (id, task_type, status, objective) "
        "VALUES ($1, 'gateway', 'schedule_failed', 'x')",
        tid,
    )
    row = await pool.fetchrow("SELECT status FROM tasks WHERE id = $1", tid)
    assert row["status"] == "schedule_failed"

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pool.execute(
            "INSERT INTO tasks (id, task_type, status, objective) "
            "VALUES ($1, 'gateway', 'bogus_status', 'x')",
            uuid.uuid4(),
        )

    # Self-cleanup: a leftover schedule_failed row would violate migration
    # 003's re-run constraint on the next fixture setup (the fixture also
    # truncates before migrations now, but tests should not litter).
    await pool.execute("DELETE FROM tasks WHERE id = $1", tid)
