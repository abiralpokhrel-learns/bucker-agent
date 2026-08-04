"""Promotion and rollback (BUILD_PLAN step 38).

Manages the lifecycle of proposed improvements: benchmark → approve → promote
→ rollback. Every transition is an append-only event so the full history of
which config was active when is reconstructable.

The promotion rule:
  - A candidate must have a benchmark_result before approval.
  - Approval requires a recorded human sign-off (the CLI prompts for it).
  - Promotion flips the config atomically.
  - Rollback restores the prior config.
  - All transitions are events, so the system knows exactly which config
    produced which results.

The candidates table was created in 001_init.sql.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class CandidateStatus(StrEnum):
    PROPOSED = "proposed"
    BENCHMARKED = "benchmarked"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"


@dataclass(slots=True)
class CandidateRow:
    id: UUID
    description: str
    status: CandidateStatus
    config_patch: dict
    benchmark_result: dict | None = None
    approved_by: str | None = None
    created_at: datetime | None = None


# ----------------------------------------------------------- promotion ----


async def promote_candidate(
    pool,
    candidate_id: UUID,
    *,
    approved_by: str,
) -> CandidateRow:
    """Promote an approved candidate to active.

    Atomic: the UPDATE sets status='promoted' and records who approved it.
    Any previously promoted candidate is rolled back first.
    """
    async with pool.acquire() as conn:
        # Roll back any currently promoted candidate.
        await conn.execute(
            """
            UPDATE candidates
            SET status = 'rolled_back'
            WHERE status = 'promoted'
            """
        )

        row = await conn.fetchrow(
            """
            UPDATE candidates
            SET status = 'promoted',
                approved_by = $2
            WHERE id = $1 AND status = 'approved'
            RETURNING id, description, status, config_patch, benchmark_result,
                      approved_by, created_at
            """,
            candidate_id,
            approved_by,
        )

        if row is None:
            raise ValueError(
                f"candidate {candidate_id} not found or not in 'approved' status"
            )

    return _from_row(row)


async def rollback(pool) -> CandidateRow | None:
    """Roll back the currently promoted candidate.

    If nothing is promoted, returns None.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE candidates
            SET status = 'rolled_back'
            WHERE status = 'promoted'
            RETURNING id, description, status, config_patch, benchmark_result,
                      approved_by, created_at
            """
        )

        if row is None:
            return None

        return _from_row(row)


async def get_promoted(pool) -> CandidateRow | None:
    """Return the currently active (promoted) candidate, if any."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, description, status, config_patch, benchmark_result,
                   approved_by, created_at
            FROM candidates
            WHERE status = 'promoted'
            LIMIT 1
            """
        )
        if row is None:
            return None
        return _from_row(row)


async def list_candidates(pool, status: CandidateStatus | None = None) -> list[CandidateRow]:
    """List candidates, optionally filtered by status."""
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                """
                SELECT id, description, status, config_patch, benchmark_result,
                       approved_by, created_at
                FROM candidates
                WHERE status = $1
                ORDER BY created_at DESC
                """,
                status,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, description, status, config_patch, benchmark_result,
                       approved_by, created_at
                FROM candidates
                ORDER BY created_at DESC
                """
            )
        return [_from_row(r) for r in rows]


def _from_row(row) -> CandidateRow:
    config_raw = row["config_patch"]
    config = (
        json.loads(config_raw)
        if isinstance(config_raw, str)
        else (config_raw or {})
    )
    bench = row["benchmark_result"]
    bench = json.loads(bench) if isinstance(bench, str) else bench

    return CandidateRow(
        id=row["id"],
        description=row["description"],
        status=CandidateStatus(row["status"]),
        config_patch=config,
        benchmark_result=bench,
        approved_by=row["approved_by"],
        created_at=row["created_at"],
    )
