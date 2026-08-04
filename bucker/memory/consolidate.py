"""Episodic -> semantic memory: distill a task's run into durable facts.

The event store IS episodic memory — every run is recorded, append-only.
Consolidation is the step that turns one episode into reusable knowledge:

  * a completed task yields facts about the project ("the test suite for
    calc.py passes after adding subtract") with source=consolidate:<id>;
  * a failed task yields a *lesson* fact (what broke and what the
    verifier said) — the same text a retry would have seen;
  * repeated failures on the same verifier produce a SKILL PROPOSAL the
    user can accept (self-improvement, human-approved: nothing is written
    to skills without a human saying yes).

Consolidation is idempotent: a task is consolidated at most once
(tracked by a marker file in memory/consolidated/).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bucker.core.eventstore import EventStore
from bucker.memory.facts import MemoryStore


@dataclass(slots=True)
class Consolidation:
    task_id: str
    facts_added: list[str] = field(default_factory=list)
    skill_proposals: list[dict] = field(default_factory=list)
    already_done: bool = False

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "facts_added": self.facts_added,
            "skill_proposals": self.skill_proposals,
            "already_done": self.already_done,
        }


class ConsolidationStore:
    """Tracks which tasks have been consolidated (one marker file each)."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path(__file__).resolve().parent.parent.parent
                     / "memory" / "consolidated")
        self.root.mkdir(parents=True, exist_ok=True)

    def is_done(self, task_id: str) -> bool:
        return (self.root / f"{task_id}.md").exists()

    def mark_done(self, task_id: str) -> None:
        (self.root / f"{task_id}.md").write_text(
            f"# consolidated\n- task: {task_id}\n", encoding="utf-8"
        )


async def consolidate_task(
    task_id: str,
    store: EventStore,
    memory: MemoryStore,
    *,
    markers: ConsolidationStore | None = None,
    force: bool = False,
) -> Consolidation:
    """Distill one task's event stream into durable facts.

    Idempotent: re-running on an already-consolidated task returns
    already_done=True and writes nothing (pass force=True to redo).
    """
    markers = markers or ConsolidationStore()
    result = Consolidation(task_id=task_id)

    if markers.is_done(task_id) and not force:
        result.already_done = True
        return result

    from uuid import UUID

    events = await store.read_stream(UUID(task_id))
    if not events:
        return result

    objective = ""
    verdict = None
    verifier = ""
    attempts = 0
    diagnostics = ""
    task_type = ""
    for e in events:
        if e.event_type == "TaskCreated":
            objective = str(e.payload.get("objective", ""))[:200]
            task_type = str(e.payload.get("task_type", ""))
        if e.event_type in ("VerificationPassed", "VerificationFailed"):
            verdict = e.event_type == "VerificationPassed"
            verifier = str(e.payload.get("verifier", ""))
            attempts = int(e.payload.get("attempt", 0) or 0)
        if e.event_type == "VerificationFailed" and not diagnostics:
            diagnostics = str(e.payload.get("diagnostics", ""))[:300]

    if not objective:
        return result

    if verdict is True:
        result.facts_added.append(
            memory.add(
                f"{task_type} task succeeded: {objective[:120]} "
                f"(verifier {verifier}, attempt {attempts})",
                source=f"consolidate:{task_id}",
            )
        )
    elif verdict is False:
        result.facts_added.append(
            memory.add(
                f"{task_type} task failed verification: {objective[:120]} — "
                f"{diagnostics[:140]}",
                source=f"consolidate:{task_id}",
            )
        )
        # Self-improvement, human-approved: propose a skill, never write it.
        result.skill_proposals.append({
            "name": "repair-after-verification-failure",
            "why": (
                f"verification failed on {verifier} after {attempts} "
                f"attempt(s) for: {objective[:100]}"
            ),
            "procedure_hint": (
                "read the verifier diagnostics, fix the first listed "
                "failure, re-run — the diagnostics are the retry prompt"
            ),
        })

    if result.facts_added or result.skill_proposals:
        markers.mark_done(task_id)
    return result
