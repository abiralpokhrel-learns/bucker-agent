"""Migration tripwires (review-driven): schema must match what code writes.

If a migration ever drops the human-review statuses (or the original
constraint is re-added without them), the approval gate silently breaks
on a real database — the API tests use a fake connection that accepts
any UPDATE, so only this tripwire catches it.
"""

from __future__ import annotations

from pathlib import Path

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
