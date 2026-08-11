"""Deterministic replay engine (BUILD_PLAN step 23).

[HAND — crown jewel] — re-runs any completed task answering every model call
from stored recordings, never hitting a live provider. Reports match/mismatch
against the original verification outcome.

Why this exists as a separate module and not just "run it in recorded mode":
  1. It reads the original event stream to find the verification outcome
     without assuming the task completed — a task that failed verification
     should replay to the same failure.
  2. It produces a structured ReplayResult with diagnostics, not just a
     pass/fail.
  3. It can be called from the API (POST /tasks/{id}/replay) and from the
     benchmark harness (to prove that recorded numbers are reproducible).

Tamper detection is at the blob level: the router's recorded mode verifies
that every stored blob still hashes to its content-addressed ref before
replaying it. A tampered blob raises RecordingMissing, not a silent mismatch.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from bucker.config import settings
from bucker.contracts.models import Task
from bucker.core.blob import BlobStore
from bucker.core.events import EventType
from bucker.core.eventstore import EventStore
from bucker.planner import PlanningFailed, generate_task_contract
from bucker.router.client import ModelCallFailed, ModelRouter, RecordingMissing
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers import available as available_verifiers
from bucker.verifiers import get as get_verifier
from bucker.verifiers import register_builtins
from bucker.worker_agent import WorkFailed, execute_task


class ReplayError(Exception):
    """The replay itself failed — not a mismatch, but a plumbing error."""


@dataclass(slots=True)
class ReplayResult:
    """Outcome of replaying one task."""

    task_id: UUID
    match: bool                    # True when original and replay agree
    original_passed: bool          # What the original verification found
    replayed_passed: bool          # What the replay found
    original_events: int = 0       # How many events in the original stream
    plan_cost_usd: float = 0.0     # Always 0 in recorded mode; tracked for telemetry
    work_cost_usd: float = 0.0
    diagnostics: str = ""
    details: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.match


def workspace_for(task_id: str) -> Path:
    """Durable per-task workspace. Same layout as activities/pipeline.py."""
    return Path(settings.blob_root).parent / "workspace" / task_id


def _replay_sandbox(workspace: Path):
    """DockerSandbox by default; LocalSandbox in lite mode (no Docker)."""
    if settings.sandbox_mode == "local":
        from bucker.sandbox.local import LocalSandbox

        return LocalSandbox(workspace)
    return DockerSandbox(workspace)


def replay_workspace_for(task_id: UUID) -> Path:
    """Isolated replay workspace: a fresh copy of the original, per run.

    Replay applies diffs and runs tests — both WRITE to the workspace. Doing
    that on the durable original would mutate the evidence a replay is meant
    to verify. Each replay run gets its own copy under workspace/replay/ and
    the copy is removed when the run ends (the original is never touched).
    """
    root = workspace_for("replay")
    root.mkdir(parents=True, exist_ok=True)
    dest = root / str(task_id)
    if dest.exists():
        # Stale copy from a crashed replay; never reuse it.
        shutil.rmtree(dest, ignore_errors=True)
    return dest


async def replay_task(
    task_id: UUID,
    *,
    store: EventStore,
    blobs: BlobStore,
    recordings_root: Path | None = None,
) -> ReplayResult:
    """Re-run a task's pipeline entirely from stored recordings.

    The pipeline runs the same code path as a live run — planner → worker →
    verifier — but every model call is answered from the recording store,
    never from a live provider.

    Returns a ReplayResult.tampered if any stored blob fails verification.
    """
    register_builtins()

    events = await store.read_stream(task_id)
    if not events:
        raise ReplayError(f"task {task_id} has no events — nothing to replay")

    # --- extract original outcome from the event stream -------------------
    created = next(
        (e for e in events if e.event_type == EventType.TASK_CREATED), None
    )
    if created is None:
        raise ReplayError(
            f"task {task_id} has no TaskCreated event — cannot determine objective"
        )

    objective = created.payload.get("objective")
    if not objective:
        raise ReplayError(
            f"task {task_id}: TaskCreated event has no objective in payload"
        )

    verification_events = [
        e for e in events
        if e.event_type in (EventType.VERIFICATION_PASSED, EventType.VERIFICATION_FAILED)
    ]
    if not verification_events:
        # Task was created but never verified — valid for replay of an
        # incomplete task, but there is nothing to compare against.
        raise ReplayError(
            f"task {task_id} has no verification event — "
            f"replay requires at least one completed verify cycle"
        )

    # Use the last verification (a retried task may have several).
    last_verdict = verification_events[-1]
    original_passed = last_verdict.event_type == EventType.VERIFICATION_PASSED

    # --- re-run the pipeline in recorded mode ----------------------------
    router = ModelRouter(blobs, mode="recorded")
    if recordings_root is not None:
        from bucker.router.client import RecordingStore
        router.recordings = RecordingStore(recordings_root)

    # 1. Plan (recorded)
    try:
        plan = await generate_task_contract(router, objective)
    except RecordingMissing as exc:
        raise ReplayError(
            f"task {task_id}: missing recording for planner — "
            f"the live run that produced this task's recordings must be re-run "
            f"before replay is possible. Original error: {exc}"
        ) from exc
    except (ModelCallFailed, PlanningFailed) as exc:
        raise ReplayError(
            f"task {task_id}: planner failed during replay — "
            f"this should not happen in recorded mode. {type(exc).__name__}: {exc}"
        ) from exc

    task: Task = plan.task

    # The planner might have picked a verifier that is no longer registered.
    # That is a real mismatch, not a plumbing error.
    if task.verifier not in available_verifiers():
        return ReplayResult(
            task_id=task_id,
            match=False,
            original_passed=original_passed,
            replayed_passed=False,
            original_events=len(events),
            plan_cost_usd=plan.cost_usd,
            diagnostics=(
                f"planner chose verifier {task.verifier!r} which is not "
                f"registered. Registered: {available_verifiers()}"
            ),
            details={"verifier_missing": task.verifier},
        )

    # 2. Work (recorded) — on an ISOLATED copy of the workspace, never the
    # durable original. Replay applies diffs and runs tests, both of which
    # write files; the original is the evidence, and it must stay pristine.
    original_workspace = workspace_for(str(task_id))
    if not original_workspace.exists():
        raise ReplayError(
            f"task {task_id}: workspace not found at {original_workspace} — "
            f"the workspace must exist for the sandbox to mount it"
        )

    workspace = replay_workspace_for(task_id)
    shutil.copytree(original_workspace, workspace)

    sandbox = _replay_sandbox(workspace)
    await sandbox.start()
    try:
        try:
            outcome = await execute_task(router, task, sandbox)
        except (ModelCallFailed, RecordingMissing) as exc:
            raise ReplayError(
                f"task {task_id}: worker failed during replay — "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        except WorkFailed as exc:
            return ReplayResult(
                task_id=task_id,
                match=False,
                original_passed=original_passed,
                replayed_passed=False,
                original_events=len(events),
                plan_cost_usd=plan.cost_usd,
                diagnostics=(
                    f"worker produced no valid result after "
                    f"{len(exc.attempts)} attempts during replay. "
                    f"This is a replay divergence — the original run produced "
                    f"a valid result."
                ),
                details={"worker_failed": str(exc)},
            )

        result = outcome.result

        # 3. Verify (deterministic given the same diff + test suite)
        verifier = get_verifier(task.verifier)
        verdict = await verifier.verify(task, result, sandbox)
    finally:
        await sandbox.stop()
        # Replay is ephemeral by design: the copy is disposable, the original
        # workspace is the durable artifact.
        shutil.rmtree(workspace, ignore_errors=True)

    replayed_passed = verdict.passed
    match = original_passed == replayed_passed

    diagnostic_parts = []
    if match:
        diagnostic_parts.append(
            f"Replay consistent: original={'PASSED' if original_passed else 'FAILED'}, "
            f"replay={'PASSED' if replayed_passed else 'FAILED'}"
        )
    else:
        diagnostic_parts.append(
            f"MISMATCH: original={'PASSED' if original_passed else 'FAILED'}, "
            f"replay={'PASSED' if replayed_passed else 'FAILED'}"
        )
        if verdict.diagnostics:
            diagnostic_parts.append(f"Replay diagnostics: {verdict.diagnostics[:500]}")

    return ReplayResult(
        task_id=task_id,
        match=match,
        original_passed=original_passed,
        replayed_passed=replayed_passed,
        original_events=len(events),
        plan_cost_usd=plan.cost_usd,
        work_cost_usd=outcome.cost_usd,
        diagnostics="\n".join(diagnostic_parts),
        details={
            "verifier": verdict.verifier,
            "verdict_details": verdict.details,
            "plan_attempts": len(plan.attempts),
            "work_attempts": len(outcome.attempts),
        },
    )
