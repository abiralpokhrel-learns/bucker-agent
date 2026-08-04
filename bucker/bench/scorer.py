"""Evaluation scorer (BUILD_PLAN step 36).

Reads recent task outcomes from the event log and scores them, surfacing
weaknesses as candidate rows for the promotion pipeline.

A "candidate" is a proposed improvement — a config change, a prompt edit,
a new tool — that might improve outcomes. This module identifies what to
improve; the promotion pipeline (step 38) decides whether to deploy it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    import asyncpg


@dataclass(slots=True)
class OutcomeScore:
    """Aggregated metrics for one task or a batch of tasks."""

    total_tasks: int = 0
    completed: int = 0
    failed: int = 0
    total_cost_usd: float = 0.0
    avg_cost_per_task: float = 0.0
    failure_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        if self.total_tasks == 0:
            return 0.0
        return self.completed / self.total_tasks


@dataclass(slots=True)
class Candidate:
    """A proposed improvement surfaced from the event log."""

    description: str
    reason: str
    evidence: dict = field(default_factory=dict)


async def score_recent_tasks(
    pool: asyncpg.Pool,
    *,
    limit: int = 50,
) -> OutcomeScore:
    """Score the most recent tasks from the event log."""
    score = OutcomeScore()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, status, objective
            FROM tasks
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )

        score.total_tasks = len(rows)
        for row in rows:
            status = row["status"]
            if status == "completed":
                score.completed += 1
            else:
                score.failed += 1
                reason = status or "unknown"
                score.failure_reasons[reason] = (
                    score.failure_reasons.get(reason, 0) + 1
                )

        # Cost aggregation from telemetry.
        cost_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(t.cost_usd), 0) as total,
                   COALESCE(AVG(t.cost_usd), 0) as avg
            FROM telemetry t
            JOIN tasks ON tasks.id = t.task_id
            WHERE tasks.created_at > NOW() - INTERVAL '7 days'
            """
        )
        if cost_row:
            score.total_cost_usd = float(cost_row["total"])
            score.avg_cost_per_task = float(cost_row["avg"])

    return score


def surface_candidates(score: OutcomeScore) -> list[Candidate]:
    """Surface improvement candidates from the scores.

    Each candidate is a data-driven suggestion — not a guess. The evidence
    field carries the specific numbers that justify it.
    """
    candidates: list[Candidate] = []

    if score.success_rate < 0.5 and score.total_tasks >= 5:
        candidates.append(Candidate(
            description="Improve planner prompt for code_change tasks",
            reason="low overall success rate",
            evidence={
                "success_rate": score.success_rate,
                "total_tasks": score.total_tasks,
            },
        ))

    if "needs_human_review" in score.failure_reasons:
        count = score.failure_reasons["needs_human_review"]
        candidates.append(Candidate(
            description="Tune max_retries and escalation thresholds",
            reason=f"{count} tasks escalated to human review",
            evidence={"escalations": count},
        ))

    if score.total_tasks >= 10 and score.success_rate > 0.8:
        top_reason = max(score.failure_reasons.items(), key=lambda x: x[1])
        candidates.append(Candidate(
            description=f"Address top failure mode: {top_reason[0]}",
            reason=f"top failure reason affects {top_reason[1]} tasks",
            evidence={
                "failure_reason": top_reason[0],
                "count": top_reason[1],
            },
        ))

    return candidates


async def create_candidate_row(
    pool: asyncpg.Pool,
    candidate: Candidate,
) -> UUID:
    """Write a candidate row to the database for the promotion pipeline."""
    import json

    row = await pool.fetchrow(
        """
        INSERT INTO candidates (description, config_patch, status)
        VALUES ($1, $2::jsonb, 'proposed')
        RETURNING id
        """,
        candidate.description,
        json.dumps({"reason": candidate.reason, **candidate.evidence}),
    )
    return row["id"]
