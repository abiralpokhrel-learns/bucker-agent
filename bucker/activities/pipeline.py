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
from bucker.core.telemetry import record_model_call, record_tool_call, record_verification
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
async def run_worker(task_id: str, task_dict: dict, attempt: int,
                     model: str | None = None) -> dict:
    """Execute one attempt at the task. The result is NOT trusted here.

    ``model`` lets adaptive planning (step 34) switch models between attempts;
    None means the configured default (settings.model).
    """
    store = await get_store()
    tid = UUID(task_id)
    task = Task(**task_dict)
    router = ModelRouter(get_blobs(), model=model)

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
            event = await store.append(
                tid,
                EventType.MODEL_CALL_COMPLETED,
                {
                    "purpose": "worker",
                    "model": att.response.model,
                    "cost_usd": att.response.cost_usd,
                    "latency_ms": att.response.latency_ms,
                    "from_recording": att.response.from_recording,
                    "usage": att.response.usage,
                },
                tool_output_ref=att.response.raw_ref,
                idempotency_key=f"{task_id}:work-{attempt}-call-{i + 1}",
            )
            async with store._pool.acquire() as conn:
                await record_model_call(
                    conn,
                    event_id=event.id,
                    task_id=tid,
                    model=att.response.model,
                    latency_ms=att.response.latency_ms,
                    cost_usd=att.response.cost_usd,
                    purpose="worker",
                    usage=att.response.usage,
                )

            # Self-critique loop: the critic verdict + any extra model calls
            # (critic pass, repair round) get their own events + telemetry.
            if att.critique_verdict is not None:
                await store.append(
                    tid,
                    EventType.CRITIQUE_COMPLETED,
                    {
                        "attempt": i + 1,
                        "verdict": att.critique_verdict,
                        "issues": att.critique_issues,
                        "repaired": att.repaired,
                    },
                    tool_output_ref=(
                        att.extra_calls[0].raw_ref if att.extra_calls else None
                    ),
                    idempotency_key=f"{task_id}:work-{attempt}-critique-{i + 1}",
                )
            for j, call in enumerate(att.extra_calls):
                call_purpose = "critic" if j == 0 else "worker"  # repair is a worker call
                call_event = await store.append(
                    tid,
                    EventType.MODEL_CALL_COMPLETED,
                    {
                        "purpose": call_purpose,
                        "model": call.model,
                        "cost_usd": call.cost_usd,
                        "latency_ms": call.latency_ms,
                        "from_recording": call.from_recording,
                        "usage": call.usage,
                    },
                    tool_output_ref=call.raw_ref,
                    idempotency_key=(
                        f"{task_id}:work-{attempt}-critic-call-{i + 1}-{j + 1}"
                    ),
                )
                async with store._pool.acquire() as conn:
                    await record_model_call(
                        conn,
                        event_id=call_event.id,
                        task_id=tid,
                        model=call.model,
                        latency_ms=call.latency_ms,
                        cost_usd=call.cost_usd,
                        purpose=call_purpose,
                        usage=call.usage,
                    )

        if outcome.applied is not None:
            tool_event = await store.append(
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
            async with store._pool.acquire() as conn:
                await record_tool_call(
                    conn,
                    event_id=tool_event.id,
                    task_id=tid,
                    tool="apply_diff",
                    latency_ms=outcome.applied.duration_ms,
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
        # (result_dict, cost_usd): the cost rides along in-band so the
        # workflow can enforce the budget — the WorkerResult dict itself must
        # stay byte-pure (the verifier reconstructs it with extra="forbid").
        return outcome.result.model_dump(), outcome.cost_usd
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

    verdict_event = await store.append(
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
    async with store._pool.acquire() as conn:
        await record_verification(
            conn,
            event_id=verdict_event.id,
            task_id=tid,
            passed=verdict.passed,
            duration_ms=verdict.duration_ms,
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


# ------------------------------------------------------------ adaptive (M3) --


@activity.defn
async def choose_adaptive_strategy(history_dict: dict) -> dict:
    """Pick the next-attempt strategy from the failure pattern (step 34).

    Wraps the pure logic in ``bucker.adaptive`` so the decision is a recorded,
    replayable step like every other policy decision. Returns:

        strategy      one of default / chunk / clarify / switch_model
        next_objective  the objective text for the next attempt
        next_model      model override for the next attempt (switch_model only)

    ``models_used`` entries may be "" meaning "the configured default model",
    which is resolved here (activities may read settings; workflow code may not).
    """
    from bucker.adaptive import (
        AttemptHistory,
        Strategy,
        choose_strategy,
        chunk_objective,
        clarify_objective,
        next_model,
    )

    current = history_dict.get("current_model") or settings.model
    history = AttemptHistory(
        attempt=int(history_dict.get("attempt", 1)),
        verifier_name=history_dict.get("verifier_name", ""),
        diagnostics=[str(d) for d in history_dict.get("diagnostics", [])],
        passed=[bool(p) for p in history_dict.get("passed", [])],
        models_used=[
            (m if m else current) for m in history_dict.get("models_used", [])
        ] or [current],
    )

    strategy = choose_strategy(history)
    objective = history_dict.get("objective", "")

    result: dict = {"strategy": str(strategy)}

    if strategy is Strategy.DEFAULT:
        # Fixed-retry behaviour: carry the verifier's diagnostics forward.
        failure_context = history_dict.get("failure_context", "")
        result["next_objective"] = (
            f"{objective}\n\n{failure_context}".strip() if failure_context else objective
        )
    elif strategy is Strategy.CHUNK:
        result["next_objective"] = chunk_objective(objective, history.diagnostics)
    elif strategy is Strategy.CLARIFY:
        result["next_objective"] = clarify_objective(objective, history.diagnostics)
    elif strategy is Strategy.SWITCH_MODEL:
        result["next_model"] = next_model(current, history.models_used)
        result["next_objective"] = objective

    return result
