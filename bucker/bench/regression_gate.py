"""Replay regression gate (BUILD_PLAN step 39).

[HAND] — before any promotion, replay a fixed suite of past workflows
under the candidate. Any previously-passing task failing → promotion
blocked + alert. This is M4: the final quality gate.

Design:
  - The gate loads a list of task IDs (the "regression suite").
  - Each is replayed via the replay engine (recorded mode, no model spend).
  - Any divergence from the original verification outcome is a block.
  - The gate returns pass/fail + a detailed report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from bucker.core.blob import BlobStore
from bucker.core.eventstore import EventStore
from bucker.replay.engine import ReplayError, ReplayResult, replay_task


class GateBlocked(Exception):
    """A regression was detected — promotion is blocked."""


@dataclass(slots=True)
class GateResult:
    """Outcome of running the regression gate."""

    passed: bool
    total: int
    matches: int
    mismatches: int
    errors: int
    results: list[ReplayResult] = field(default_factory=list)

    @property
    def report(self) -> str:
        lines = [
            "=" * 64,
            "REGRESSION GATE RESULT",
            "=" * 64,
            f"Tasks replayed:  {self.total}",
            f"Matches:         {self.matches}",
            f"Mismatches:      {self.mismatches}",
            f"Errors:          {self.errors}",
            f"Gate:            {'PASSED' if self.passed else 'BLOCKED'}",
            "",
        ]
        for r in self.results:
            if not r.match:
                lines.append(
                    f"  MISMATCH {r.task_id}: "
                    f"original={'PASSED' if r.original_passed else 'FAILED'} "
                    f"replay={'PASSED' if r.replayed_passed else 'FAILED'}"
                )
        return "\n".join(lines)


async def run_regression_gate(
    task_ids: list[UUID],
    *,
    store: EventStore,
    blobs: BlobStore,
) -> GateResult:
    """Replay a suite of tasks and block on any regression.

    Args:
        task_ids: UUIDs of previously-completed tasks to replay.
        store: EventStore for reading original events.
        blobs: BlobStore for reading stored recordings.

    Returns:
        GateResult with pass/fail + per-task details.

    Raises:
        GateBlocked if any previously-passing task now fails (caller should
        block the promotion and alert).
    """
    results: list[ReplayResult] = []
    matches = 0
    mismatches = 0
    errors = 0

    for task_id in task_ids:
        try:
            result = await replay_task(
                task_id,
                store=store,
                blobs=blobs,
            )
            results.append(result)
            if result.match:
                matches += 1
            else:
                mismatches += 1
        except ReplayError as exc:
            results.append(ReplayResult(
                task_id=task_id,
                match=False,
                original_passed=False,
                replayed_passed=False,
                diagnostics=str(exc),
                details={"error": str(exc)},
            ))
            errors += 1

    total = len(task_ids)
    passed = mismatches == 0 and errors == 0

    gate = GateResult(
        passed=passed,
        total=total,
        matches=matches,
        mismatches=mismatches,
        errors=errors,
        results=results,
    )

    if not passed:
        raise GateBlocked(gate.report)

    return gate
