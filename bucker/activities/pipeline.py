"""Worker and verifier activities — the impure half of the Phase 1 loop.

Sandbox lifecycle, deliberately: the **workspace is durable state on the host
disk, keyed by task id; the container is ephemeral and per-activity.** A
container that had to survive between activities would be a second kind of
state to recover after a crash, competing with the event log. Instead, a
restarted worker re-creates a container over the same workspace and continues.
That is the same principle as the event log itself — keep exactly one source of
truth and re-derive everything else.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from temporalio import activity

from bucker.activities.demo import get_blobs, get_store
from bucker.config import settings
from bucker.contracts.models import Task, WorkerResult
from bucker.core.events import EventType
from bucker.retry import Action, AttemptState, decide
from bucker.router.client import ModelRouter
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers import get as get_verifier
from bucker.verifiers import register_builtins
from bucker.worker_agent import WorkFailed, execute_task

register_builtins()


def workspace_for(task_id: str) -> Path:
    """Durable per-task workspace. Survives crashes; containers do not."""
    return Path(settings.blob_root).parent / "workspace" / task_id


# ----------------------------------------------------------------- worker ---
@activity.defn
async def run_worker(task_id: str, task_dict: dict, attempt: int) -> dict:
    """Execute one attempt at the task. The result is NOT trusted here."""
    store = await get_store()
    tid = UUID(task_id)
    task = Task(**task_dict)
    router = ModelRouter(get_blobs())

    sandbox = DockerSandbox(workspace_for(task_id))
    await sandbox.start()
    try:
        try:
            outcome = await execute_task(router, task, sandbox)
        except WorkFailed as exc:
            for i, att in enumerate(exc.attempts):
                await store.append(
                    tid,
                    EventType.SCHEMA_VALIDATION_FAILED,
                    {"component": "worker", "attempt": i + 1, "errors": att.errors},
                    tool_output_ref=att.response.raw_ref,
                    idempotency_key=f"{task_id}:work-{attempt}-invalid-{i + 1}",
                )
            raise

        for i, att in enumerate(outcome.attempts):
            await store.append(
                tid,
                EventType.MODEL_CALL_COMPLETED,
                {
                    "purpose": "worker",
                    "model": att.response.model,
                    "cost_usd": att.response.cost_usd,
                    "latency_ms": att.response.latency_ms,
                    "from_recording": att.response.from_recording,
                },
                tool_output_ref=att.response.raw_ref,
                idempotency_key=f"{task_id}:work-{attempt}-call-{i + 1}",
            )

        if outcome.applied is not None:
            await store.append(
                tid,
                EventType.TOOL_CALL_COMPLETED,
                {
                    "tool": "apply_diff",
                    "exit_code": outcome.applied.exit_code,
                    "secrets_redacted": len(outcome.applied.secret_findings),
                },
                tool_output_ref=get_blobs().put_json({
                    "stdout": outcome.applied.stdout,
                    "stderr": outcome.applied.stderr,
                }),
                idempotency_key=f"{task_id}:work-{attempt}-apply",
            )

        result_ref = get_blobs().put_json(outcome.result.model_dump())
        await store.append(
            tid,
            EventType.WORKER_COMPLETED,
            {
                "attempt": attempt,
                "status": outcome.result.status,
                "summary": outcome.result.summary,
                "cost_usd": outcome.cost_usd,
            },
            tool_output_ref=result_ref,
            idempotency_key=f"{task_id}:work-{attempt}-completed",
        )
        return outcome.result.model_dump()
    finally:
        await sandbox.stop()


# --------------------------------------------------------------- verifier ---
@activity.defn
async def run_verifier(task_id: str, task_dict: dict, result_dict: dict,
                       attempt: int) -> dict:
    """Run the task's registered verifier. This is where a claim becomes a fact."""
    store = await get_store()
    tid = UUID(task_id)
    task = Task(**task_dict)
    result = WorkerResult(**result_dict)

    await store.append(
        tid,
        EventType.VERIFICATION_REQUESTED,
        {"verifier": task.verifier, "attempt": attempt},
        idempotency_key=f"{task_id}:verify-{attempt}-requested",
    )

    verifier = get_verifier(task.verifier)

    sandbox = DockerSandbox(workspace_for(task_id))
    await sandbox.start()
    try:
        verdict = await verifier.verify(task, result, sandbox)
    finally:
        await sandbox.stop()

    await store.append(
        tid,
        EventType.VERIFICATION_PASSED if verdict.passed else EventType.VERIFICATION_FAILED,
        {
            "verifier": verdict.verifier,
            "attempt": attempt,
            "duration_ms": verdict.duration_ms,
            "details": verdict.details,
        },
        tool_output_ref=get_blobs().put(verdict.diagnostics),
        idempotency_key=f"{task_id}:verify-{attempt}-{'pass' if verdict.passed else 'fail'}",
    )

    return {
        "passed": verdict.passed,
        "verifier": verdict.verifier,
        "diagnostics": verdict.diagnostics,
        "details": verdict.details,
    }


# ------------------------------------------------------------- transitions --
@activity.defn
async def record_decision(task_id: str, decision_dict: dict, attempt: int) -> None:
    """Write the policy's decision into the log before acting on it."""
    store = await get_store()
    tid = UUID(task_id)
    action = decision_dict["action"]
    reason = decision_dict["reason"]

    event = {
        Action.RETRY: EventType.RETRY_SCHEDULED,
        Action.ESCALATE: EventType.NEEDS_HUMAN_REVIEW,
        Action.COMPLETE: EventType.TASK_COMPLETED,
        Action.HALT: EventType.BUDGET_EXCEEDED,
    }[Action(action)]

    if event is EventType.BUDGET_EXCEEDED and "deadline" in reason:
        event = EventType.DEADLINE_EXCEEDED

    await store.append(
        tid,
        event,
        {"attempt": attempt, "reason": reason},
        idempotency_key=f"{task_id}:decision-{attempt}-{action}",
    )


@activity.defn
async def evaluate_policy(state_dict: dict) -> dict:
    """Apply the retry policy.

    An activity rather than inline workflow code purely so the decision is
    replayable as a recorded step; the logic itself is pure (bucker/retry.py).
    """
    decision = decide(AttemptState(**state_dict))
    return {
        "action": str(decision.action),
        "reason": decision.reason,
        "failure_context": decision.failure_context,
    }
