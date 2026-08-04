"""SWE-bench integration and paired runner tests (steps 26-27).

Pure tests cover instance parsing and data structures. Integration tests
need Docker + Postgres and are skipped without them.
"""

from __future__ import annotations

import json

from bucker.bench.runner import ExperimentRun, TaskResult
from bucker.bench.swebench import (
    SWEInstance,
    prediction_from_diff,
)

# --------------------------------------------------------- instance parsing --


def test_swe_instance_from_dict():
    d = {
        "instance_id": "django__django-10097",
        "repo": "django/django",
        "base_commit": "abc123def456",
        "problem_statement": "Fix the thing.",
        "test_patch": "--- a/x\n+++ b/x\n",
        "hints_text": "Look at the view.",
        "version": "3.1",
        "FAIL_TO_PASS": ["test_a"],
        "PASS_TO_PASS": ["test_b"],
    }
    inst = SWEInstance.from_dict(d)
    assert inst.instance_id == "django__django-10097"
    assert inst.repo == "django/django"
    assert inst.base_commit == "abc123def456"
    assert inst.problem_statement == "Fix the thing."
    assert inst.test_patch == "--- a/x\n+++ b/x\n"
    assert inst.fail_to_pass == ["test_a"]
    assert inst.pass_to_pass == ["test_b"]


def test_swe_instance_from_minimal_dict():
    """Some fields are optional."""
    d = {
        "instance_id": "x__x-1",
        "repo": "x/x",
        "base_commit": "abc",
        "problem_statement": "fix",
        "test_patch": "",
    }
    inst = SWEInstance.from_dict(d)
    assert inst.hints_text == ""
    assert inst.fail_to_pass == []


# ------------------------------------------------------ prediction format ----


def test_prediction_from_diff():
    pred = prediction_from_diff("--- a/x\n+++ b/x\n", "django__django-1")
    assert pred["instance_id"] == "django__django-1"
    assert pred["model_patch"] == "--- a/x\n+++ b/x\n"
    assert "model_name_or_path" in pred


def test_prediction_includes_model_name():
    pred = prediction_from_diff("diff", "id", "gpt-4")
    assert pred["model_name_or_path"] == "gpt-4"


# ------------------------------------------------------- experiment run ----


def test_experiment_run_serializable():
    run = ExperimentRun(
        run_id="test-123",
        model="test-model",
        instances=1,
        bucker_results=[
            TaskResult(
                instance_id="a__a-1", architecture="bucker",
                success=True, diff="x", cost_usd=0.05, elapsed_s=10.0,
            )
        ],
        baseline_results=[
            TaskResult(
                instance_id="a__a-1", architecture="baseline",
                success=False, diff="y", cost_usd=0.03, elapsed_s=5.0,
            )
        ],
    )

    data = run.to_dict()
    json.dumps(data)  # must not raise
    assert data["bucker_success_rate"] == 1.0
    assert data["baseline_success_rate"] == 0.0
    assert data["bucker_cost_total"] == 0.05
    assert data["baseline_cost_total"] == 0.03


def test_experiment_run_success_rates():
    run = ExperimentRun(
        run_id="t", model="m", instances=4,
        bucker_results=[
            TaskResult(instance_id="1", architecture="bucker", success=True),
            TaskResult(instance_id="2", architecture="bucker", success=False),
            TaskResult(instance_id="3", architecture="bucker", success=True),
            TaskResult(instance_id="4", architecture="bucker", success=True),
        ],
        baseline_results=[
            TaskResult(instance_id="1", architecture="baseline", success=True),
            TaskResult(instance_id="2", architecture="baseline", success=True),
            TaskResult(instance_id="3", architecture="baseline", success=False),
            TaskResult(instance_id="4", architecture="baseline", success=False),
        ],
    )
    assert run.bucker_success_rate == 0.75
    assert run.baseline_success_rate == 0.5


def test_experiment_run_handles_errors():
    """Results with errors are excluded from success rate."""
    run = ExperimentRun(
        run_id="t", model="m", instances=2,
        bucker_results=[
            TaskResult(instance_id="1", architecture="bucker", success=True),
            TaskResult(instance_id="2", architecture="bucker", success=False,
                       error="clone failed"),
        ],
        baseline_results=[
            TaskResult(instance_id="1", architecture="baseline", success=True),
            TaskResult(instance_id="2", architecture="baseline", success=False,
                       error="clone failed"),
        ],
    )
    assert run.bucker_success_rate == 1.0  # only resolved count
    assert run.baseline_success_rate == 1.0


def test_experiment_run_cost_totals():
    run = ExperimentRun(
        run_id="t", model="m", instances=2,
        bucker_results=[
            TaskResult(instance_id="1", architecture="bucker", success=False, cost_usd=0.10),
            TaskResult(instance_id="2", architecture="bucker", success=False, cost_usd=0.05),
        ],
        baseline_results=[
            TaskResult(instance_id="1", architecture="baseline", success=False, cost_usd=0.03),
            TaskResult(instance_id="2", architecture="baseline", success=False, cost_usd=0.02),
        ],
    )
    assert run.bucker_cost_total == 0.15
    assert run.baseline_cost_total == 0.05
