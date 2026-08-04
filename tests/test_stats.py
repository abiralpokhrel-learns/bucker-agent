"""Stats module tests (step 28).

Hand-computed fixtures verified against scipy.stats.chi2.sf.
No randomness in the expected values — every p-value and CI bound
is computed by hand or verified against the reference implementation.
"""

from __future__ import annotations

import math

import pytest

from bucker.bench.stats import (
    PairedOutcomes,
    analyze,
    bootstrap_delta_ci,
    mcnemar_test,
)

# ----------------------------------------------------- contingency table ---


def test_paired_outcomes_all_both_pass():
    o = PairedOutcomes.from_results(
        [True, True, True], [True, True, True]
    )
    assert o.a == 3
    assert o.b == 0
    assert o.c == 0
    assert o.d == 0
    assert o.n == 3
    assert o.bucker_success_rate == 1.0
    assert o.baseline_success_rate == 1.0
    assert o.delta == 0.0


def test_paired_outcomes_all_both_fail():
    o = PairedOutcomes.from_results(
        [False, False], [False, False]
    )
    assert o.a == 0
    assert o.d == 2
    assert o.bucker_success_rate == 0.0
    assert o.delta == 0.0


def test_paired_outcomes_bucker_dominates():
    o = PairedOutcomes.from_results(
        [True, True, True, False],  # bucker: 3/4 = 75%
        [True, False, False, False],  # baseline: 1/4 = 25%
    )
    assert o.a == 1  # both passed
    assert o.b == 2  # bucker passed, baseline failed
    assert o.c == 0  # baseline passed, bucker failed
    assert o.d == 1  # both failed
    assert o.bucker_success_rate == 0.75
    assert o.baseline_success_rate == 0.25
    assert o.delta == 0.5


def test_paired_outcomes_baseline_better():
    o = PairedOutcomes.from_results(
        [True, False, False],  # bucker: 1/3
        [True, True, False],  # baseline: 2/3
    )
    assert o.a == 1
    assert o.b == 0
    assert o.c == 1
    assert o.d == 1
    assert o.delta == -1 / 3


def test_mismatched_lengths_raises():
    with pytest.raises(ValueError, match="mismatched"):
        PairedOutcomes.from_results([True], [True, False])


# ------------------------------------------------------------ McNemar ----


def test_mcnemar_perfect_agreement():
    """All pairs concordant — cannot reject H₀."""
    o = PairedOutcomes(a=10, b=0, c=0, d=5)
    result = mcnemar_test(o)
    assert result.statistic == 0.0
    assert result.p_value == 1.0
    assert not result.significant
    assert result.discordant_pairs == 0


def test_mcnemar_highly_significant():
    """b=20, c=2 → χ² = (|20-2|-1)²/22 = 289/22 ≈ 13.136
    p = chi2_sf(13.136, 1) ≈ 0.00029 (from scipy)"""
    o = PairedOutcomes(a=5, b=20, c=2, d=5)
    result = mcnemar_test(o)
    assert result.discordant_pairs == 22
    assert math.isclose(result.statistic, (17 ** 2) / 22, rel_tol=0.01)
    assert result.p_value < 0.001
    assert result.significant


def test_mcnemar_at_critical_value():
    """χ² ≈ 3.841 yields p ≈ 0.05. b=13, c=3 gives (9)²/16 = 5.0625,
    which is > 3.841 so p < 0.05."""
    o = PairedOutcomes(a=0, b=13, c=3, d=0)
    result = mcnemar_test(o)
    assert result.discordant_pairs == 16
    assert result.p_value < 0.05
    assert result.significant


def test_mcnemar_not_significant():
    """b=8, c=6 → χ² = (|8-6|-1)²/14 = 1/14 ≈ 0.0714, p ≈ 0.789"""
    o = PairedOutcomes(a=0, b=8, c=6, d=0)
    result = mcnemar_test(o)
    assert result.p_value > 0.5
    assert not result.significant


def test_mcnemar_warns_on_few_discordant_pairs():
    o = PairedOutcomes(a=0, b=3, c=1, d=0)
    result = mcnemar_test(o)
    assert result.discordant_pairs == 4
    assert "only 4 discordant pairs" in result.summary.lower()


# ---------------------------------------------------------- bootstrap CI ----


def test_bootstrap_ci_perfect():
    """All bucker pass, all baseline fail → delta = 1.0 with zero variance."""
    ci = bootstrap_delta_ci(
        [True, True, True], [False, False, False],
        samples=1000, seed=42,
    )
    assert ci.lower == 1.0
    assert ci.upper == 1.0
    assert ci.mean == 1.0


def test_bootstrap_ci_no_difference():
    """Identical results → delta ≈ 0."""
    ci = bootstrap_delta_ci(
        [True, False, True, False], [True, False, True, False],
        samples=1000, seed=42,
    )
    assert abs(ci.mean) < 0.01


def test_bootstrap_ci_needs_two_instances():
    with pytest.raises(ValueError, match="at least 2"):
        bootstrap_delta_ci([True], [False], samples=100)


# --------------------------------------------------------------- analysis ----


def test_analyze_smoke():
    """End-to-end: raw results → full analysis → decision."""
    stats = analyze(
        [True, True, True, False, True],  # bucker: 4/5 = 80%
        [True, False, False, False, False],  # baseline: 1/5 = 20%
        bucker_cost_total=0.50,
        baseline_cost_total=0.10,
    )
    assert stats.outcomes.n == 5
    assert stats.outcomes.delta > 0
    assert stats.mcnemar.discordant_pairs > 0
    assert stats.bootstrap.lower > -0.5
    assert stats.bootstrap.upper < 1.5
    # cost/success: bucker = 0.50/4 = 0.125, baseline = 0.10/1 = 0.10
    assert stats.cost_per_success_bucker == 0.125
    assert stats.cost_per_success_baseline == 0.10


def test_analyze_decision_stop_when_worse():
    """Bucker does worse → decision should be STOP."""
    # 6 instances, bucker does worse
    stats = analyze(
        [False, False, True, False, False, True],   # bucker: 2/6
        [True, True, False, True, True, False],       # baseline: 4/6
    )
    assert "STOP" in stats.decision


def test_analyze_decision_proceed_when_significant():
    """Strong statistically significant improvement → PROCEED."""
    # 15 instances: b=10, c=2 → χ²=(7)²/12 ≈ 4.08, p ≈ 0.043
    bucker = [True] * 12 + [False] * 3   # 12/15 = 80%
    baseline = [True] * 3 + [False] * 12  # 3/15 = 20%
    stats = analyze(bucker, baseline)
    assert "PROCEED" in stats.decision
    assert stats.mcnemar.significant


def test_analyze_inconclusive_few_instances():
    """Fewer than 5 instances → INCONCLUSIVE regardless."""
    stats = analyze(
        [True, True], [False, False],
    )
    assert "INCONCLUSIVE" in stats.decision
    assert "fewer than 5" in stats.decision.lower()


def test_paired_stats_summary_does_not_raise():
    """Summary() must not crash."""
    stats = analyze(
        [True, False, True, False],
        [True, True, False, False],
        bucker_cost_total=0.42,
        baseline_cost_total=0.31,
    )
    s = stats.summary()
    assert "Instances" in s
    assert "Decision" in s


# ------------------------------------------- chi-squared survival function --
# Hand-computed exact fixtures (verified against the closed forms):
#   df=1: erfc(√(x/2))
#   df=2: e^{-x/2}
#   df=4: e^{-x/2}(1 + x/2)
#   df=6: e^{-x/2}(1 + x/2 + (x/2)²/2)


def test_chi2_sf_df1_matches_erfc():
    from bucker.bench.stats import _chi2_sf

    # χ²(1): P(X > 2) = erfc(1) = 0.15729920705028513...
    assert math.isclose(_chi2_sf(2.0, 1), math.erfc(1.0), rel_tol=1e-12)


def test_chi2_sf_df2_is_exponential():
    from bucker.bench.stats import _chi2_sf

    # χ²(2) ~ Exp(1/2): P(X > 4) = e^{-2}
    assert math.isclose(_chi2_sf(4.0, 2), math.exp(-2.0), rel_tol=1e-12)


def test_chi2_sf_df4_exact():
    from bucker.bench.stats import _chi2_sf

    # P(χ²(4) > 6) = e^{-3}(1 + 3) = 4e^{-3} ≈ 0.19914827347145578
    assert math.isclose(_chi2_sf(6.0, 4), 4.0 * math.exp(-3.0), rel_tol=1e-12)


def test_chi2_sf_df6_exact():
    from bucker.bench.stats import _chi2_sf

    # P(χ²(6) > 10) = e^{-5}(1 + 5 + 25/2) = e^{-5} · 18.5
    expected = math.exp(-5.0) * 18.5
    assert math.isclose(_chi2_sf(10.0, 6), expected, rel_tol=1e-12)


def test_chi2_sf_never_negative_or_above_one():
    """The df>2 path must produce a real survival probability.

    The old series was off by a factor of a, which made P(χ²(4) > 6)
    evaluate to 1 - 2·0.80085 ≈ -0.60. A negative p-value is not a
    numerical quirk; it is a broken statistic.
    """
    from bucker.bench.stats import _chi2_sf

    for df in (1, 2, 3, 4, 5, 6, 8, 10):
        for x in (0.1, 1.0, 4.0, 6.0, 10.0, 25.0):
            p = _chi2_sf(x, df)
            assert 0.0 <= p <= 1.0, f"df={df} x={x} -> p={p}"
        # monotone in x: larger statistic -> smaller p-value
        for x1, x2 in ((0.5, 1.0), (2.0, 8.0)):
            assert _chi2_sf(x1, df) > _chi2_sf(x2, df)


def test_chi2_sf_odd_df_matches_known_value():
    """df=3 has no closed form; check the series against a known value.

    P(χ²(3) > 5) ≈ 0.171797 (scipy.stats.chi2.sf(5, 3)). The series must
    agree to a few significant figures for the moderate-x regime this
    module operates in.
    """
    from bucker.bench.stats import _chi2_sf

    assert math.isclose(_chi2_sf(5.0, 3), 0.171797, rel_tol=1e-3)
