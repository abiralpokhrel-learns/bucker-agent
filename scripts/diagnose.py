"""Failure diagnosis (BUILD_PLAN step 29).

Reads the experiment log, connects to Postgres to pull event streams for
each failed task, and produces a failure taxonomy. This is the product
working for itself — diagnosing its own failures from the audit trail.

Usage:
    uv run python -m scripts.diagnose
    uv run python -m scripts.diagnose --run-id abc123
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from bucker.bench.runner import EXPERIMENT_LOG
from bucker.config import settings
from bucker.core.eventstore import EventStore, create_pool


@dataclass
class FailureRecord:
    instance_id: str
    architecture: str
    reason: str
    events: list[str] = field(default_factory=list)
    event_count: int = 0


@dataclass
class Taxonomy:
    total_instances: int
    bucker_passed: int
    baseline_passed: int
    failures: list[FailureRecord] = field(default_factory=list)

    @property
    def bucker_failure_rate(self) -> float:
        if self.total_instances == 0:
            return 0.0
        return 1.0 - (self.bucker_passed / self.total_instances)

    @property
    def baseline_failure_rate(self) -> float:
        if self.total_instances == 0:
            return 0.0
        return 1.0 - (self.baseline_passed / self.total_instances)

    def categories(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for f in self.failures:
            if "clone failed" in f.reason.lower():
                counts["clone/prep error"] += 1
            elif "db error" in f.reason.lower():
                counts["database error"] += 1
            elif "PlanningFailed" in f.reason:
                counts["planner failed"] += 1
            elif "WorkFailed" in f.reason:
                counts["worker produced no valid result"] += 1
            elif "VerificationFailed" in str(f.events):
                counts["verification failed"] += 1
            elif "RuntimeError" in f.reason or "Exception" in f.reason:
                counts["unexpected error"] += 1
            else:
                counts["other/unknown"] += 1
        return dict(counts)

    def report(self) -> str:
        lines = [
            "=" * 64,
            "FAILURE TAXONOMY",
            "=" * 64,
            f"Instances: {self.total_instances}",
            f"Bucker passed:   {self.bucker_passed}/{self.total_instances} "
            f"({1 - self.bucker_failure_rate:.0%})",
            f"Baseline passed: {self.baseline_passed}/{self.total_instances} "
            f"({1 - self.baseline_failure_rate:.0%})",
            "",
            "Failure categories:",
        ]
        for category, count in sorted(
            self.categories().items(), key=lambda x: -x[1]
        ):
            lines.append(f"  {category:<40} {count}")

        lines.append("")
        lines.append("Per-instance detail:")
        for f in self.failures:
            lines.append(
                f"  [{f.architecture}] {f.instance_id}: {f.reason[:100]}"
            )
            if f.events:
                lines.append(f"    event types: {', '.join(f.events[:8])}")

        return "\n".join(lines)


async def diagnose(
    run_id: str | None = None,
    log_path: Path = EXPERIMENT_LOG,
) -> Taxonomy:
    """Load experiment results and diagnose failures from event streams."""
    if not log_path.exists():
        print(f"No experiment log at {log_path}", file=sys.stderr)
        return Taxonomy(total_instances=0, bucker_passed=0, baseline_passed=0)

    # Load the most recent run, or a specific one.
    runs = []
    for line in log_path.read_text(encoding="utf-8").strip().splitlines():
        if not line.strip():
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if run_id:
        runs = [r for r in runs if r.get("run_id") == run_id]
    if not runs:
        print("No runs found in experiment log.", file=sys.stderr)
        return Taxonomy(total_instances=0, bucker_passed=0, baseline_passed=0)

    run = runs[-1]  # most recent
    instances = run.get("instances", 0)

    bucker_passed = sum(
        1 for r in run.get("bucker_results", []) if r.get("success")
    )
    baseline_passed = sum(
        1 for r in run.get("baseline_results", []) if r.get("success")
    )

    failures: list[FailureRecord] = []

    # Connect to DB to read event streams for failed bucker tasks.
    try:
        pool = await create_pool(settings.database_url)
        store = EventStore(pool)
        try:
            for r in run.get("bucker_results", []):
                if r.get("success"):
                    continue
                if r.get("error"):
                    failures.append(FailureRecord(
                        instance_id=r["instance_id"],
                        architecture="bucker",
                        reason=r["error"],
                    ))
                    continue
                # Try to read the event stream for more detail.
                try:
                    tid_str = r.get("task_id")
                    if tid_str:
                        events = await store.read_stream(UUID(tid_str))
                        failures.append(FailureRecord(
                            instance_id=r["instance_id"],
                            architecture="bucker",
                            reason=f"verification failed ({len(events)} events)",
                            events=[e.event_type for e in events],
                            event_count=len(events),
                        ))
                    else:
                        failures.append(FailureRecord(
                            instance_id=r["instance_id"],
                            architecture="bucker",
                            reason="verification failed (no task_id in log)",
                        ))
                except Exception as exc:
                    failures.append(FailureRecord(
                        instance_id=r["instance_id"],
                        architecture="bucker",
                        reason=str(exc),
                    ))
        finally:
            await pool.close()
    except Exception as exc:
        print(f"Could not connect to database: {exc}", file=sys.stderr)
        # Still build taxonomy from the log alone.
        for r in run.get("bucker_results", []):
            if not r.get("success"):
                failures.append(FailureRecord(
                    instance_id=r["instance_id"],
                    architecture="bucker",
                    reason=r.get("error", "verification failed"),
                ))

    for r in run.get("baseline_results", []):
        if not r.get("success"):
            failures.append(FailureRecord(
                instance_id=r["instance_id"],
                architecture="baseline",
                reason=r.get("error", "verification failed"),
            ))

    return Taxonomy(
        total_instances=instances,
        bucker_passed=bucker_passed,
        baseline_passed=baseline_passed,
        failures=failures,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="bucker failure diagnosis")
    parser.add_argument(
        "--run-id", help="diagnose a specific run (default: most recent)"
    )
    parser.add_argument(
        "--log", type=Path, default=EXPERIMENT_LOG,
        help="path to experiments.jsonl",
    )
    args = parser.parse_args()

    taxonomy = await diagnose(args.run_id, args.log)
    print(taxonomy.report())
    return 0 if not taxonomy.failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
