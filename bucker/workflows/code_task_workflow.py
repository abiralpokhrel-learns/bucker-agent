"""The Phase 1 workflow: plan -> work -> verify -> retry -> escalate.

[HAND] — Temporal determinism rules apply to every line, same as the Phase 0
workflow. No I/O, no clock, no randomness here; all of that lives in activities.

Read this file as the shape of the argument the whole project is making:

    plan            fuzzy goal becomes a typed contract
    work            a claim is produced
    verify          the claim meets an objective check
    decide          pass -> done, fail -> retry with the diagnostics,
                    out of retries -> a human, over budget -> stop
    (repeat)

Nothing here decides policy. The workflow asks ``evaluate_policy`` and executes
the answer, so the rules stay in one pure, unit-tested module instead of being
smeared through orchestration code where they cannot be tested.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from bucker.activities.pipeline import (
        choose_adaptive_strategy,
        consolidate_task_memory,
        evaluate_policy,
        record_decision,
        record_failure,
        run_verifier,
        run_worker,
    )
    from bucker.activities.planner import plan_task
    from bucker.core.budget import pre_spend_decision
    from bucker.retry import Action

from temporalio.exceptions import ActivityError


@dataclass
class CodeTaskInput:
    task_id: str
    objective: str
    max_retries: int = 2
    budget_usd: float | None = None
    deadline_minutes: int | None = None
    #: Conservative reserve per model call for the pre-spend guard. The
    #: workflow halts when cost_so_far + this estimate would exceed the
    #: budget, so a single expensive call cannot overshoot a tight budget
    #: unchecked. Not a charge — the actual cost is reconciled after the
    #: call returns. 0.0 = only halt on already-spent cost.
    step_estimate_usd: float = 0.02
    #: M3 (step 34): vary strategy on repeated failure (switch model / chunk /
    #: clarify) instead of always re-prompting with diagnostics. Fixed retry
    #: stays the default so the two can be A/B tested against each other.
    adaptive: bool = False


@workflow.defn
class CodeTaskWorkflow:
    def __init__(self) -> None:
        self._phase = "created"
        self._attempt = 0
        self._cost_usd = 0.0
        self._cost_unknown = False
        self._step_estimate = 0.02  # set from input in run()
        # Adaptive-planning history. Model entries are "" until a switch
        # happens — "" resolves to the configured default in the activity.
        self._passed: list[bool] = []
        self._diagnostics: list[str] = []
        self._models_used: list[str] = []
        self._current_model: str | None = None

    # Short timeouts because worker death must be detected fast; long-running
    # activities get heartbeats instead of a bigger number here (see the M1
    # entry in docs/decisions.md).
    def _opts(self, minutes: int = 10) -> dict:
        return {
            "start_to_close_timeout": timedelta(minutes=minutes),
            "retry_policy": RetryPolicy(
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30),
                maximum_attempts=3,
            ),
        }

    def _pre_spend_decision(
        self,
        started: datetime,
        budget: float | None,
        deadline: int | None,
        attempt: int,
    ) -> dict | None:
        """Halt BEFORE the next model spend, not after it.

        ``evaluate_policy`` can only react to cost after a call returns; an
        expensive worker call would still happen on the way to a HALT. This
        guard runs first: if the budget or deadline is already breached
        (plus a conservative reserve for the call about to fire), the
        workflow records a HALT decision and never fires the next activity
        that would spend. Pure and deterministic — elapsed time comes from
        ``workflow.now()``, never the wall clock.

        Budget ordering note: the budget is taken from the planner's
        contract unless the caller supplied one — so the PLANNER itself is
        not budget-guarded (it runs before a budget exists). The guard
        applies to everything after planning. For a hard ceiling from the
        first token, pass budget_usd explicitly.
        """
        elapsed_minutes = (workflow.now() - started).total_seconds() / 60.0
        return pre_spend_decision(
            self._cost_usd, budget, elapsed_minutes, deadline, attempt,
            next_step_estimate=self._step_estimate,
            cost_unknown=self._cost_unknown,
        )

    async def _halt(self, task_id: str, decision: dict, attempt: int) -> dict:
        """Record the HALT decision and return the workflow's terminal state."""
        await workflow.execute_activity(
            record_decision,
            args=[task_id, decision, attempt],
            **self._opts(2),
        )
        self._phase = "halted"
        await self._remember(task_id)
        return {
            "status": "halted",
            "attempts": self._attempt,
            "reason": decision["reason"],
        }

    async def _remember(self, task_id: str) -> None:
        """Episodic -> semantic memory: distill this run into durable facts.

        Fire-and-forget with a short timeout — a memory failure must never
        fail the task it is remembering.
        """
        with contextlib.suppress(Exception):
            await workflow.execute_activity(
                consolidate_task_memory,
                args=[task_id],
                start_to_close_timeout=timedelta(seconds=60),
            )

    async def _fail(self, task_id: str, reason: str, attempt: int) -> dict:
        """Terminal failure: record TASK_FAILED and return a failed state.

        The retry policy decides between pass/retry/escalate/halt based on
        VERIFICATION results. When an activity itself fails permanently
        (all model providers down, sandbox unreachable, worker crash), no
        policy decision exists — the workflow would previously crash with
        an unhandled ActivityError, leaving the task row stuck in
        ``in_progress`` forever. This records the terminal event so the API
        and dashboard reflect reality.
        """
        with contextlib.suppress(Exception):
            await workflow.execute_activity(
                record_failure,
                args=[task_id, reason, attempt],
                **self._opts(2),
            )
        self._phase = "failed"
        return {"status": "failed", "attempts": attempt, "reason": reason}

    @workflow.run
    async def run(self, inp: CodeTaskInput) -> dict:
        started = workflow.now()
        self._step_estimate = inp.step_estimate_usd

        # --- plan --------------------------------------------------------
        self._phase = "planning"
        try:
            task_dict, plan_cost, plan_unknown = await workflow.execute_activity(
                plan_task, args=[inp.task_id, inp.objective], **self._opts(5)
            )
        except ActivityError as exc:
            return await self._fail(
                inp.task_id, f"planning failed: {exc.message}", 0
            )
        self._cost_usd += float(plan_cost or 0.0)
        self._cost_unknown = self._cost_unknown or bool(plan_unknown)

        budget = inp.budget_usd or task_dict.get("budget_usd")
        deadline = inp.deadline_minutes or task_dict.get("deadline_minutes")

        # --- work / verify / decide -------------------------------------
        last_verdict: dict = {}

        for attempt in range(1, inp.max_retries + 2):
            self._attempt = attempt

            # Halt BEFORE the next model spend: if the budget or deadline is
            # already breached, an expensive worker call must not happen on
            # the way to a HALT that was already earned.
            pre = self._pre_spend_decision(started, budget, deadline, attempt)
            if pre is not None:
                return await self._halt(inp.task_id, pre, attempt)

            self._phase = "working"
            try:
                result_dict, worker_cost, worker_unknown = await workflow.execute_activity(
                    run_worker,
                    args=[inp.task_id, task_dict, attempt, self._current_model],
                    **self._opts(15),
                )
            except ActivityError as exc:
                # A permanently-failed worker activity (all providers down,
                # sandbox broken) has no policy answer — record it as a
                # terminal failure instead of crashing the workflow.
                return await self._fail(
                    inp.task_id,
                    f"worker activity failed: {exc.message}",
                    attempt,
                )
            self._cost_usd += float(worker_cost or 0.0)
            self._cost_unknown = self._cost_unknown or bool(worker_unknown)

            self._phase = "verifying"
            try:
                last_verdict = await workflow.execute_activity(
                    run_verifier,
                    args=[inp.task_id, task_dict, result_dict, attempt],
                    **self._opts(15),
                )
            except ActivityError as exc:
                # Verifier infra failure (sandbox unreachable, verifier
                # crash) is not a verification result — terminal failure.
                return await self._fail(
                    inp.task_id,
                    f"verifier activity failed: {exc.message}",
                    attempt,
                )

            # Record what this attempt was, for the adaptive history.
            self._passed.append(bool(last_verdict["passed"]))
            self._diagnostics.append(str(last_verdict.get("diagnostics", "")))
            self._models_used.append(self._current_model or "")

            elapsed_minutes = (workflow.now() - started).total_seconds() / 60.0

            decision = await workflow.execute_activity(
                evaluate_policy,
                {
                    "attempt": attempt,
                    "max_retries": inp.max_retries,
                    "verification_passed": last_verdict["passed"],
                    "diagnostics": last_verdict.get("diagnostics", ""),
                    "cost_usd": self._cost_usd,
                    "budget_usd": budget,
                    "elapsed_minutes": elapsed_minutes,
                    "deadline_minutes": deadline,
                },
                **self._opts(2),
            )

            await workflow.execute_activity(
                record_decision,
                args=[inp.task_id, decision, attempt],
                **self._opts(2),
            )

            action = Action(decision["action"])

            if action is Action.COMPLETE:
                self._phase = "completed"
                await self._remember(inp.task_id)
                return {"status": "completed", "attempts": attempt,
                        "verdict": last_verdict}

            if action is Action.ESCALATE:
                self._phase = "needs_human_review"
                await self._remember(inp.task_id)
                return {"status": "needs_human_review", "attempts": attempt,
                        "reason": decision["reason"], "verdict": last_verdict}

            if action is Action.HALT:
                self._phase = "halted"
                return {"status": "halted", "attempts": attempt,
                        "reason": decision["reason"]}

            # RETRY: feed the specific failure back into the next attempt, so
            # the next attempt is a correction rather than another draw from
            # the same distribution. With adaptive planning (M3) the strategy
            # itself varies: switch model, chunk, or clarify.
            if inp.adaptive:
                # choose_adaptive_strategy is itself a model call — the
                # budget guard applies to it too.
                pre = self._pre_spend_decision(
                    started, budget, deadline, attempt + 1
                )
                if pre is not None:
                    return await self._halt(inp.task_id, pre, attempt)
                strategy = await workflow.execute_activity(
                    choose_adaptive_strategy,
                    {
                        "attempt": attempt,
                        "verifier_name": last_verdict.get("verifier", ""),
                        "objective": task_dict["objective"],
                        "failure_context": decision["failure_context"],
                        "diagnostics": list(self._diagnostics),
                        "passed": list(self._passed),
                        "models_used": list(self._models_used),
                        "current_model": self._current_model,
                    },
                    **self._opts(2),
                )
                next_objective = strategy.get("next_objective")
                if next_objective:
                    task_dict = {**task_dict, "objective": next_objective}
                if strategy.get("next_model"):
                    self._current_model = strategy["next_model"]
            else:
                task_dict = {
                    **task_dict,
                    "objective": (
                        f"{task_dict['objective']}\n\n{decision['failure_context']}"
                    ),
                }

        # The loop bound and the policy agree, so this is unreachable; if it
        # ever fires, the two have drifted and a human should look.
        self._phase = "needs_human_review"
        return {"status": "needs_human_review", "attempts": self._attempt,
                "reason": "retry loop exhausted without a terminal decision",
                "verdict": last_verdict}

    @workflow.query
    def phase(self) -> str:
        return self._phase

    @workflow.query
    def attempt(self) -> int:
        return self._attempt
