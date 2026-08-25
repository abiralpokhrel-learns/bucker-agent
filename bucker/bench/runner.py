"""Paired benchmark runner (BUILD_PLAN step 27).

Runs N SWE-bench instances through BOTH the bucker architecture and the
baseline single-agent loop on the same model. Writes every result to the
experiment log so the numbers are reproducible.

The fairness rules baked into the code:
  - Same model (via the same ModelRouter)
  - Same sandbox image and isolation settings
  - Same instances, same workspace seeding
  - Same evaluation harness and grading
  - Bucker gets planner+worker+verifier; baseline gets the simple loop
  - Both produce diffs; the same verifier decides truth

Nothing here is opinions about which is better. The code runs both, records
everything, and the stats module (step 28) computes significance later.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from bucker.bench.baseline import run_baseline
from bucker.bench.swebench import (
    SWEInstance,
    clone_instance,
    first_n_instances,
    prediction_from_diff,
    run_evaluation,
)
from bucker.config import settings
from bucker.core.blob import BlobStore
from bucker.core.eventstore import EventStore, create_pool
from bucker.planner import PlanningFailed, generate_task_contract
from bucker.router.client import ModelRouter
from bucker.sandbox.runtime import DockerSandbox
from bucker.verifiers import get as get_verifier
from bucker.verifiers import register_builtins
from bucker.worker_agent import WorkFailed, execute_task

EXPERIMENT_LOG = Path(settings.blob_root).parent / "experiments.jsonl"


class RunnerError(Exception):
    """The runner itself failed — not the models, but the plumbing."""


# ---------------------------------------------------------------- data types --


@dataclass(slots=True)
class TaskResult:
    """One architecture's run on one instance."""

    instance_id: str
    architecture: str  # "bucker" or "baseline"
    success: bool  # True if the verifier passed
    diff: str | None = None
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    error: str | None = None
    details: dict = field(default_factory=dict)


@dataclass(slots=True)
class ExperimentRun:
    """One paired comparison run."""

    run_id: str
    model: str
    instances: int
    bucker_results: list[TaskResult] = field(default_factory=list)
    baseline_results: list[TaskResult] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    evaluation_report: list[dict] = field(default_factory=list)

    @property
    def bucker_success_rate(self) -> float:
        resolved = [r for r in self.bucker_results if not r.error]
        if not resolved:
            return 0.0
        return sum(1 for r in resolved if r.success) / len(resolved)

    @property
    def baseline_success_rate(self) -> float:
        resolved = [r for r in self.baseline_results if not r.error]
        if not resolved:
            return 0.0
        return sum(1 for r in resolved if r.success) / len(resolved)

    @property
    def bucker_cost_total(self) -> float:
        return round(sum(r.cost_usd for r in self.bucker_results), 6)

    @property
    def baseline_cost_total(self) -> float:
        return round(sum(r.cost_usd for r in self.baseline_results), 6)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "model": self.model,
            "instances": self.instances,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "bucker_success_rate": self.bucker_success_rate,
            "baseline_success_rate": self.baseline_success_rate,
            "bucker_cost_total": self.bucker_cost_total,
            "baseline_cost_total": self.baseline_cost_total,
            "bucker_results": [
                {
                    "instance_id": r.instance_id,
                    "success": r.success,
                    "cost_usd": r.cost_usd,
                    "elapsed_s": r.elapsed_s,
                    "error": r.error,
                }
                for r in self.bucker_results
            ],
            "baseline_results": [
                {
                    "instance_id": r.instance_id,
                    "success": r.success,
                    "cost_usd": r.cost_usd,
                    "elapsed_s": r.elapsed_s,
                    "error": r.error,
                }
                for r in self.baseline_results
            ],
            "evaluation_report": self.evaluation_report,
        }


# ----------------------------------------------------------------- main run --


async def run_paired_benchmark(
    *,
    instances: list[SWEInstance] | None = None,
    n: int = 5,
    model: str | None = None,
    output_path: Path | None = None,
) -> ExperimentRun:
    """Run N SWE-bench instances through both architectures.

    Requires Docker, Postgres, and a model (API key or local). Each instance
    is cloned, run through bucker (planner → worker → verifier), then through
    the baseline (simple agent loop). Both produce diffs that are evaluated
    by the official SWE-bench harness.
    """
    register_builtins()

    if instances is None:
        instances = first_n_instances(n)

    model_name = model or settings.model
    run_id = uuid4().hex[:12]

    run = ExperimentRun(
        run_id=run_id,
        model=model_name,
        instances=len(instances),
        started_at=time.time(),
    )

    # --- setup shared infrastructure ---------------------------------------
    pool = await create_pool(settings.database_url)
    store = EventStore(pool)
    blobs = BlobStore(settings.blob_root)
    router = ModelRouter(blobs, model=model_name, mode=settings.model_mode)

    try:
        for instance in instances:
            print(f"\n{'=' * 64}")
            print(f"INSTANCE {instance.instance_id}")
            print(f"  repo: {instance.repo} @ {instance.base_commit[:8]}")

            workspace = _workspace_for(run_id, instance.instance_id)
            task_id = uuid4()

            try:
                clone_instance(instance, workspace)
            except Exception as exc:
                _record_failure(run, instance.instance_id, str(exc))
                continue

            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO tasks (id, task_type, objective, status) "
                        "VALUES ($1, 'code_change', $2, 'pending')",
                        task_id, instance.problem_statement,
                    )
                await store.append(
                    task_id, "TaskCreated",
                    {
                        "objective": instance.problem_statement,
                        "task_type": "code_change",
                        "instance_id": instance.instance_id,
                    },
                    idempotency_key=f"{task_id}:created",
                )
            except Exception as exc:
                _record_failure(run, instance.instance_id, f"db error: {exc}")
                continue

            # ---- bucker --------------------------------------------------
            bucker_result = await _run_bucker(
                instance, task_id, workspace, router, blobs, store
            )
            run.bucker_results.append(bucker_result)

            # ---- baseline ------------------------------------------------
            # Reset workspace to base commit for a clean comparison.
            try:
                clone_instance(instance, workspace)
            except Exception as exc:
                baseline_result = TaskResult(
                    instance_id=instance.instance_id,
                    architecture="baseline",
                    success=False,
                    error=str(exc),
                )
            else:
                baseline_result = await _run_baseline(
                    instance, workspace, router
                )
            run.baseline_results.append(baseline_result)

            print(f"  bucker:   {'PASS' if bucker_result.success else 'FAIL'}"
                  f"  ${bucker_result.cost_usd:.4f}")
            print(f"  baseline: {'PASS' if baseline_result.success else 'FAIL'}"
                  f"  ${baseline_result.cost_usd:.4f}")

        # ---- evaluate both via official harness ---------------------------
        predictions_path = _write_predictions(run)
        if not predictions_path:
            print("\n  no diffs produced — skipping evaluation")
            run.evaluation_report = [{
                "error": "no predictions: neither architecture produced a diff",
            }]
        else:
            try:
                report = run_evaluation(
                    predictions_path,
                    instances_path=settings.blob_root.parent / "swebench_lite.json",
                )
                run.evaluation_report = report
            except Exception as exc:
                print(f"\n  evaluation error: {exc}")
                run.evaluation_report = [{"error": str(exc)}]

    finally:
        await pool.close()

    run.finished_at = time.time()

    # ---- write experiment log --------------------------------------------
    output = Path(output_path or EXPERIMENT_LOG)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "a", encoding="utf-8") as f:
        f.write(json.dumps(run.to_dict()) + "\n")

    print(f"\n{'=' * 64}")
    print(f"RUN {run_id} COMPLETE")
    print(f"  instances:   {run.instances}")
    print(f"  bucker:      {run.bucker_success_rate:.0%}  "
          f"${run.bucker_cost_total:.4f}")
    print(f"  baseline:    {run.baseline_success_rate:.0%}  "
          f"${run.baseline_cost_total:.4f}")
    print(f"  log:         {output}")

    return run


# ---------------------------------------------------------------- internal --


def _workspace_for(run_id: str, instance_id: str) -> Path:
    return Path(settings.blob_root).parent / "workspace" / run_id / instance_id


def _record_failure(run: ExperimentRun, instance_id: str, error: str) -> None:
    """Record a pre-model failure (clone, DB, etc.) for both architectures."""
    run.bucker_results.append(TaskResult(
        instance_id=instance_id, architecture="bucker",
        success=False, error=error,
    ))
    run.baseline_results.append(TaskResult(
        instance_id=instance_id, architecture="baseline",
        success=False, error=error,
    ))


async def _run_bucker(
    instance: SWEInstance,
    task_id,
    workspace: Path,
    router: ModelRouter,
    blobs: BlobStore,
    store: EventStore,
) -> TaskResult:
    """Run the bucker pipeline on one instance."""
    started = time.time()
    try:
        plan = await generate_task_contract(router, instance.problem_statement)
        task = plan.task

        sandbox = DockerSandbox(workspace)
        await sandbox.start()
        try:
            outcome = await execute_task(router, task, sandbox)
            result = outcome.result

            verifier = get_verifier(task.verifier)
            verdict = await verifier.verify(task, result, sandbox)

            cost = plan.cost_usd + outcome.cost_usd
            success = verdict.passed

            return TaskResult(
                instance_id=instance.instance_id,
                architecture="bucker",
                success=success,
                diff=result.diff,
                cost_usd=cost,
                elapsed_s=time.time() - started,
                details={
                    "plan_attempts": len(plan.attempts),
                    "work_attempts": len(outcome.attempts),
                    "verifier": verdict.verifier,
                },
            )
        finally:
            await sandbox.stop()

    except (PlanningFailed, WorkFailed) as exc:
        return TaskResult(
            instance_id=instance.instance_id,
            architecture="bucker",
            success=False,
            cost_usd=0.0,
            elapsed_s=time.time() - started,
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:
        return TaskResult(
            instance_id=instance.instance_id,
            architecture="bucker",
            success=False,
            error=str(exc),
        )


async def _run_baseline(
    instance: SWEInstance,
    workspace: Path,
    router: ModelRouter,
) -> TaskResult:
    """Run the baseline single-agent loop on one instance."""
    started = time.time()
    try:
        sandbox = DockerSandbox(workspace)
        await sandbox.start()
        try:
            result = await run_baseline(
                router,
                instance.problem_statement,
                sandbox,
            )
            return TaskResult(
                instance_id=instance.instance_id,
                architecture="baseline",
                success=result.passed,
                diff=result.final_diff,
                cost_usd=result.total_cost_usd,
                elapsed_s=time.time() - started,
                details={
                    "iterations": result.total_iterations,
                    "status": result.status,
                },
            )
        finally:
            await sandbox.stop()
    except Exception as exc:
        return TaskResult(
            instance_id=instance.instance_id,
            architecture="baseline",
            success=False,
            error=str(exc),
        )


def _write_predictions(run: ExperimentRun) -> Path | None:
    """Write predictions in the SWE-bench format for both architectures.

    Returns None when neither architecture produced a diff (the harness
    crashes on an empty predictions file, so it must not be invoked).
    """
    predictions = []
    for r in run.bucker_results:
        if r.diff:
            predictions.append(prediction_from_diff(
                r.diff, r.instance_id, f"bucker-{run.model}"
            ))
    for r in run.baseline_results:
        if r.diff:
            predictions.append(prediction_from_diff(
                r.diff, r.instance_id, f"baseline-{run.model}"
            ))

    if not predictions:
        return None

    path = Path(settings.blob_root).parent / "predictions" / f"{run.run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(predictions, indent=2), encoding="utf-8")
    return path
