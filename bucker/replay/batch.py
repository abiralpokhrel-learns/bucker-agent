"""Batch replay: prove reproducibility across many tasks at once.

Single-task replay answers "does THIS result reproduce?". The M2 gate
and everyday regression paranoia need the fleet-level answer: replay the
last N completed tasks and report how many MATCH. A match rate below
1.0 is a red flag with names attached — that is what this module emits.

Errors never mask verdicts: a task whose recordings are missing counts
as an ERROR (its own bucket), not as a mismatch, because "cannot
re-check" and "checked and diverged" demand different responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(slots=True)
class BatchReplayReport:
    """Fleet-level replay outcome. Pure data; rendering lives elsewhere."""

    requested: int = 0
    matched: list[str] = field(default_factory=list)
    mismatched: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return len(self.matched) + len(self.mismatched) + len(self.errors)

    @property
    def match_rate(self) -> float | None:
        checked = len(self.matched) + len(self.mismatched)
        if checked == 0:
            return None
        return len(self.matched) / checked

    def summary_line(self) -> str:
        rate = self.match_rate
        rate_text = f"{rate:.0%}" if rate is not None else "n/a"
        return (
            f"replay: {self.attempted}/{self.requested} attempted — "
            f"{len(self.matched)} match, {len(self.mismatched)} MISMATCH, "
            f"{len(self.errors)} error (match rate {rate_text})"
        )


async def replay_batch(
    pool: Any,
    store: Any,
    blobs: Any,
    *,
    limit: int = 25,
    status_filter: str = "completed",
    task_ids: list[str] | None = None,
    replay_fn: Any = None,
) -> BatchReplayReport:
    """Replay up to ``limit`` tasks newest-first (or an explicit id list).

    ``replay_fn`` is injectable for tests; production passes the real
    bucker.replay.engine.replay_task.
    """
    from bucker.replay.engine import ReplayError
    from bucker.replay.engine import replay_task as default_replay

    replay = replay_fn or default_replay
    report = BatchReplayReport()

    if task_ids:
        ids = [UUID(t) for t in task_ids]
    else:
        rows = await pool.fetch(
            """
            SELECT id FROM tasks WHERE status = $1
            ORDER BY created_at DESC LIMIT $2
            """,
            status_filter,
            int(limit),
        )
        ids = [row["id"] for row in rows]

    report.requested = len(ids)
    for tid in ids:
        try:
            result = await replay(tid, store=store, blobs=blobs)
        except ReplayError as exc:
            report.errors.append({"task_id": str(tid),
                                  "reason": str(exc)[:200]})
            continue
        except Exception as exc:  # noqa: BLE001 — one bad task must not stop the batch
            report.errors.append({
                "task_id": str(tid),
                "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
            })
            continue
        if result.match:
            report.matched.append(str(tid))
        else:
            report.mismatched.append({
                "task_id": str(tid),
                "original_passed": result.original_passed,
                "replayed_passed": result.replayed_passed,
                "diagnostics": (result.diagnostics or "")[:300],
            })
    return report


def format_batch_report(report: BatchReplayReport) -> str:
    """Human-readable batch outcome (pure)."""
    lines = [report.summary_line()]
    for m in report.mismatched:
        lines.append(f"  MISMATCH {m['task_id'][:8]} "
                     f"(original={'PASSED' if m['original_passed'] else 'FAILED'}, "
                     f"replay={'PASSED' if m['replayed_passed'] else 'FAILED'})")
        if m["diagnostics"]:
            lines.append(f"    {m['diagnostics'][:160]}")
    for e in report.errors:
        lines.append(f"  ERROR    {e['task_id'][:8]} — {e['reason']}")
    return "\n".join(lines)
