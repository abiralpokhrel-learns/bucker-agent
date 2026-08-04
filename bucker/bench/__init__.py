"""Benchmark harness (BUILD_PLAN steps 25-30).

The credibility asset: a paired comparison of this architecture against a
simple single-agent baseline on identical SWE-bench instances, same model,
same tools. Reports success rate, cost, latency with real statistics.

Step 25: Baseline single-agent loop (baseline.py).
Step 26: SWE-bench integration (swebench.py).
Step 27: Paired benchmark runner (runner.py).
Step 28: Stats module — McNemar, bootstrap CI (stats.py).
Step 29: Iteration loop — benchmark -> candidates -> promote (promotion.py, regression_gate.py).
Step 30: THE GATE — M2 — scripts/m2_gate.py, the go/no-go decision rule.

The gate is IMPLEMENTED but has not yet PASSED with published live numbers
(see README: the project is prototype until then). Experiment logs land in
evaluation_results/ (git-ignored) and are exported deliberately, with
recordings, when a result is published — negative results included.
"""

from bucker.bench.baseline import (
    BaselineIteration,
    BaselineResult,
    run_baseline,
)
from bucker.bench.runner import (
    ExperimentRun,
    TaskResult,
    run_paired_benchmark,
)
from bucker.bench.stats import (
    BootstrapCI,
    McNemarResult,
    PairedOutcomes,
    PairedStats,
    analyze,
    bootstrap_delta_ci,
    mcnemar_test,
)
from bucker.bench.swebench import (
    SWEBenchError,
    SWEInstance,
    clone_instance,
    first_n_instances,
    load_instances,
    prediction_from_diff,
)

__all__ = [
    "analyze",
    "BaselineIteration",
    "BaselineResult",
    "BootstrapCI",
    "bootstrap_delta_ci",
    "clone_instance",
    "ExperimentRun",
    "first_n_instances",
    "load_instances",
    "McNemarResult",
    "mcnemar_test",
    "PairedOutcomes",
    "PairedStats",
    "prediction_from_diff",
    "run_baseline",
    "run_paired_benchmark",
    "SWEBenchError",
    "SWEInstance",
    "TaskResult",
]
