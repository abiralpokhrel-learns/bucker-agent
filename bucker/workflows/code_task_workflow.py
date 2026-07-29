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

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from bucker.activities.pipeline import (
        evaluate_policy,
        record_decision,
        run_verifier,
        run_worker,
    )
    from bucker.activities.planner import plan_task
    from bucker.retry import Action


@dataclass
class CodeTaskInput:
    task_id: str
    objective: str
    max_retries: int = 2
    budget_usd: float | None = None
    deadline_minutes: int | None = None


@workflow.defn
class CodeTaskWorkflow:
    def __init__(self) -> None:
        self._phase = "created"
        self._attempt = 0
        self._cost_usd = 0.0

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

    @workflow.run
    async def run(self, inp: CodeTaskInput) -> dict:
        started = workflow.now()

        # --- plan --------------------------------------------------------
        self._phase = "planning"
        task_dict = await workflow.execute_activity(
            plan_task, args=[inp.task_id, inp.objective], **self._opts(5)
        )

        budget = inp.budget_usd or task_dict.get("budget_usd")
        deadline = inp.deadline_minutes or task_dict.get("deadline_minutes")

        # --- work / verify / decide -------------------------------------
        last_verdict: dict = {}

        for attempt in range(1, inp.max_retries + 2):
            self._attempt = attempt

            self._phase = "working"
            result_dict = await workflow.execute_activity(
                run_worker, args=[inp.task_id, task_dict, attempt], **self._opts(15)
            )

            self._phase = "verifying"
            last_verdict = await workflow.execute_activity(
                run_verifier,
                args=[inp.task_id, task_dict, result_dict, attempt],
                **self._opts(15),
            )

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
                return {"status": "completed", "attempts": attempt,
                        "verdict": last_verdict}

            if action is Action.ESCALATE:
                self._phase = "needs_human_review"
                return {"status": "needs_human_review", "attempts": attempt,
                        "reason": decision["reason"], "verdict": last_verdict}

            if action is Action.HALT:
                self._phase = "halted"
                return {"status": "halted", "attempts": attempt,
                        "reason": decision["reason"]}

            # RETRY: feed the specific failure back into the next plan, so the
            # next attempt is a correction rather than another draw from the
            # same distribution.
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
