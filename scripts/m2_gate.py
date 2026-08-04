"""THE GATE — M2 (BUILD_PLAN step 30).

Run the full paired benchmark with a specified model, apply the decision
rule from the stats module, and produce a publishable summary.

This is the moment the BUILD_PLAN has been building toward: a reproducible,
statistically-rigorous comparison of the bucker architecture against a
simple single-agent baseline on identical SWE-bench instances.

The decision rule (from bucker/bench/stats.py):
  Proceed only on statistically meaningful success-rate improvement
  OR clearly favorable cost/success tradeoff. Publish either way.

Usage:
    uv run python -m scripts.m2_gate --instances 25 --model openrouter/anthropic/claude-sonnet-4
    uv run python -m scripts.m2_gate --instances 5  # smoke test

Requirements:
    - Docker running (sandbox + SWE-bench evaluation)
    - Postgres running + migrations applied
    - API key set (OPENROUTER_API_KEY or equivalent)
    - Sandbox image built (docker build -f Dockerfile.sandbox -t bucker-sandbox:latest .)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from bucker.bench.runner import EXPERIMENT_LOG, run_paired_benchmark
from bucker.bench.stats import analyze
from bucker.config import settings


def rule(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}", flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="M2 GATE — paired benchmark + decision"
    )
    parser.add_argument(
        "--instances", type=int, default=25,
        help="number of SWE-bench Lite instances (default: 25)",
    )
    parser.add_argument(
        "--model", default=settings.model,
        help=f"model to use for both systems (default: {settings.model})",
    )
    parser.add_argument(
        "--run-id", default=None,
        help="custom run ID (default: auto-generated)",
    )
    args = parser.parse_args()

    rule(f"M2 GATE :: paired benchmark on {args.instances} instances")
    print(f"  model:     {args.model}")
    print(f"  image:     {settings.sandbox_image}")
    print(f"  mode:      {settings.model_mode}")
    print(f"  started:   {datetime.now(UTC).isoformat()}")
    print(f"  log:       {EXPERIMENT_LOG}")

    if args.instances < 5:
        print("\n  WARNING: fewer than 5 instances — stats will be inconclusive.")
        print("  This is a smoke test, not a publishable result.")

    # --- run the paired benchmark ------------------------------------------
    rule("RUNNING PAIRED BENCHMARK")

    try:
        run = await run_paired_benchmark(
            n=args.instances,
            model=args.model,
        )
    except Exception as exc:
        print(f"\nFATAL: benchmark failed: {exc}", file=sys.stderr)
        return 2

    # --- statistical analysis ----------------------------------------------
    rule("STATISTICAL ANALYSIS")

    bucker_passed = [r.success for r in run.bucker_results if not r.error]
    baseline_passed = [r.success for r in run.baseline_results if not r.error]

    if len(bucker_passed) < 5:
        print("Too few resolved instances for meaningful statistics.")
        print(f"  Bucker errors:   {sum(1 for r in run.bucker_results if r.error)}")
        print(f"  Baseline errors: {sum(1 for r in run.baseline_results if r.error)}")
        return 1

    stats = analyze(
        bucker_passed,
        baseline_passed,
        bucker_cost_total=run.bucker_cost_total,
        baseline_cost_total=run.baseline_cost_total,
    )

    print(stats.summary())

    # --- publishable output ------------------------------------------------
    rule("PUBLISHABLE SUMMARY")

    publish = {
        "title": f"Bucker Agent M2 Benchmark — {args.model}",
        "date": datetime.now(UTC).isoformat(),
        "model": args.model,
        "instances": args.instances,
        "bucker_success_rate": stats.outcomes.bucker_success_rate,
        "baseline_success_rate": stats.outcomes.baseline_success_rate,
        "delta": stats.outcomes.delta,
        "mcnemar_p_value": stats.mcnemar.p_value,
        "mcnemar_significant": stats.mcnemar.significant,
        "bootstrap_ci_lower": stats.bootstrap.lower,
        "bootstrap_ci_upper": stats.bootstrap.upper,
        "bucker_cost_total": run.bucker_cost_total,
        "baseline_cost_total": run.baseline_cost_total,
        "decision": stats.decision,
        "methodology": (
            "Paired comparison on SWE-bench Lite instances. Same model, same "
            "sandbox, same tools. Bucker uses planner+worker+verifier; baseline "
            "uses a simple single-agent loop. Both produce diffs graded by the "
            "official SWE-bench harness. McNemar's test for statistical "
            "significance, bootstrap CI for effect size."
        ),
    }

    output_path = EXPERIMENT_LOG.parent / f"m2_{run.run_id}.json"
    import json
    output_path.write_text(json.dumps(publish, indent=2), encoding="utf-8")
    print(f"  Published: {output_path}")

    # --- the decision ------------------------------------------------------
    rule("DECISION")
    print(f"  {stats.decision}")

    if "STOP" in stats.decision:
        return 1
    if "PROCEED" in stats.decision:
        return 0
    return 0  # INCONCLUSIVE — not a failure, just needs more data


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
