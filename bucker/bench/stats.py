"""Stats module (BUILD_PLAN step 28).

[HAND — never vibe your stats] — McNemar's test and bootstrap confidence
intervals for paired binary outcomes. Pure math, no I/O, no randomness at
the top level. The only external dependency is math.

Why McNemar:
  Paired binary outcomes on the SAME instances violate independence.
  A chi-squared or t-test would give wrong p-values because the two
  architectures are run on identical inputs. McNemar's test accounts for
  the pairing and is the standard choice for this exact design.

Why bootstrap CI:
  McNemar's test says whether the difference is statistically significant.
  A confidence interval on the success-rate delta says how big the
  difference actually is. Together they give the reader everything needed
  to judge the result.

The decision rule (applied in step 30):
  Proceed only on statistically meaningful success-rate improvement OR
  clearly favorable cost/success tradeoff. Publish either way.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


# ---------------------------------------------------------------- data types --


@dataclass(frozen=True, slots=True)
class PairedOutcomes:
    """Contingency table for one paired comparison.

    Each row is a matched pair (same instance, both systems).

        baseline_pass  baseline_fail
    bucker_pass      a              b
    bucker_fail      c              d

    a = both passed
    b = bucker passed, baseline failed  (the ones we want)
    c = baseline passed, bucker failed  (the ones that hurt)
    d = both failed
    """

    a: int  # both passed
    b: int  # bucker passed, baseline failed
    c: int  # baseline passed, bucker failed
    d: int  # both failed

    @classmethod
    def from_results(
        cls,
        bucker_passed: Sequence[bool],
        baseline_passed: Sequence[bool],
    ) -> PairedOutcomes:
        """Build the contingency table from raw results.

        The two sequences must be aligned: index i is the same instance
        in both lists.
        """
        if len(bucker_passed) != len(baseline_passed):
            raise ValueError(
                f"mismatched lengths: {len(bucker_passed)} vs "
                f"{len(baseline_passed)}"
            )
        a = b = c = d = 0
        for bp, bl in zip(bucker_passed, baseline_passed, strict=True):
            if bp and bl:
                a += 1
            elif bp and not bl:
                b += 1
            elif not bp and bl:
                c += 1
            else:
                d += 1
        return cls(a=a, b=b, c=c, d=d)

    @property
    def n(self) -> int:
        return self.a + self.b + self.c + self.d

    @property
    def bucker_success_rate(self) -> float:
        if self.n == 0:
            return 0.0
        return (self.a + self.b) / self.n

    @property
    def baseline_success_rate(self) -> float:
        if self.n == 0:
            return 0.0
        return (self.a + self.c) / self.n

    @property
    def delta(self) -> float:
        """Success-rate delta: positive means bucker is better."""
        return self.bucker_success_rate - self.baseline_success_rate


@dataclass(frozen=True, slots=True)
class McNemarResult:
    """Result of McNemar's test for paired binary outcomes."""

    statistic: float
    p_value: float
    significant: bool  # True at alpha=0.05
    discordant_pairs: int  # b + c (the pairs that matter)

    @property
    def summary(self) -> str:
        if self.discordant_pairs < 10:
            return (
                f"McNemar: χ²={self.statistic:.3f}, p={self.p_value:.4f} "
                f"(WARNING: only {self.discordant_pairs} discordant pairs — "
                f"the approximation is unreliable below ~25)"
            )
        sig = "SIGNIFICANT" if self.significant else "not significant"
        return (
            f"McNemar: χ²={self.statistic:.3f}, p={self.p_value:.4f} "
            f"({sig} at α=0.05, {self.discordant_pairs} discordant pairs)"
        )


@dataclass(frozen=True, slots=True)
class BootstrapCI:
    """Bootstrap confidence interval for the success-rate delta."""

    lower: float
    upper: float
    mean: float
    confidence: float = 0.95
    samples: int = 10_000

    @property
    def summary(self) -> str:
        return (
            f"Bootstrap {self.confidence:.0%} CI for delta: "
            f"[{self.lower:.3f}, {self.upper:.3f}] "
            f"(mean={self.mean:.3f}, {self.samples} samples)"
        )


@dataclass(frozen=True, slots=True)
class PairedStats:
    """Complete statistical analysis of a paired comparison."""

    outcomes: PairedOutcomes
    mcnemar: McNemarResult
    bootstrap: BootstrapCI
    bucker_cost_total: float
    baseline_cost_total: float

    @property
    def cost_per_success_bucker(self) -> float:
        successes = self.outcomes.a + self.outcomes.b
        if successes == 0:
            return float("inf")
        return self.bucker_cost_total / successes

    @property
    def cost_per_success_baseline(self) -> float:
        successes = self.outcomes.a + self.outcomes.c
        if successes == 0:
            return float("inf")
        return self.baseline_cost_total / successes

    @property
    def decision(self) -> str:
        """Apply the M2 decision rule.

        Proceed only on statistically meaningful success-rate improvement
        OR clearly favorable cost/success tradeoff.
        """
        if self.outcomes.n < 5:
            return "INCONCLUSIVE: fewer than 5 instances — run more"

        if self.mcnemar.significant and self.outcomes.delta > 0:
            return "PROCEED: statistically significant improvement in success rate"

        if (
            self.outcomes.delta > 0
            and self.mcnemar.discordant_pairs >= 25
            and self.cost_per_success_bucker <= self.cost_per_success_baseline
        ):
            return (
                "PROCEED: favorable cost/success tradeoff "
                "(not statistically significant but cheaper per success)"
            )

        if self.outcomes.delta <= 0:
            return (
                "STOP: bucker does not improve success rate "
                f"(delta={self.outcomes.delta:.3f})"
            )

        return (
            "INCONCLUSIVE: improvement not significant, "
            "cost tradeoff not clearly favorable — more data or rethink"
        )

    def summary(self) -> str:
        lines = [
            f"Instances: {self.outcomes.n}",
            f"Bucker:    {self.outcomes.bucker_success_rate:.1%}  "
            f"${self.bucker_cost_total:.2f} total, "
            f"${self.cost_per_success_bucker:.2f}/success",
            f"Baseline:  {self.outcomes.baseline_success_rate:.1%}  "
            f"${self.baseline_cost_total:.2f} total, "
            f"${self.cost_per_success_baseline:.2f}/success",
            f"Delta:     {self.outcomes.delta:+.1%}",
            "",
            self.mcnemar.summary,
            self.bootstrap.summary,
            "",
            f"Decision: {self.decision}",
        ]
        return "\n".join(lines)


# ------------------------------------------------------------ McNemar test ----


def mcnemar_test(outcomes: PairedOutcomes) -> McNemarResult:
    """McNemar's test for paired binary outcomes.

    Tests H₀: the two systems (bucker and baseline) have the same success
    rate, against H₁: they differ (two-sided).

    Uses Yates' continuity correction (the |b - c| - 1 term), which is
    the standard recommendation for small samples.
    """
    b, c = outcomes.b, outcomes.c
    discordant = b + c

    if discordant == 0:
        # No discordant pairs — the systems agree on every instance.
        # This is a degenerate case: χ² = 0, p = 1.0 (cannot reject H₀).
        return McNemarResult(
            statistic=0.0,
            p_value=1.0,
            significant=False,
            discordant_pairs=0,
        )

    # Yates' correction for continuity
    statistic = (abs(b - c) - 1) ** 2 / discordant

    # p-value from the chi-squared distribution with 1 degree of freedom.
    # Two-sided by construction (McNemar's test is inherently two-sided;
    # the "direction" is in the sign of b - c, which we report separately
    # as the delta).
    p_value = _chi2_sf(statistic, df=1)

    return McNemarResult(
        statistic=statistic,
        p_value=p_value,
        significant=p_value < 0.05,
        discordant_pairs=discordant,
    )


# --------------------------------------------------------- bootstrap CI -------


def bootstrap_delta_ci(
    bucker_passed: list[bool],
    baseline_passed: list[bool],
    *,
    confidence: float = 0.95,
    samples: int = 10_000,
    seed: int | None = 42,
) -> BootstrapCI:
    """Bootstrap confidence interval for the success-rate delta.

    Resamples matched pairs with replacement, computes delta for each
    resample, and takes percentiles. Seed is fixed so the CI is
    reproducible — set seed=None for true randomness.
    """
    n = len(bucker_passed)
    if n < 2:
        raise ValueError("need at least 2 instances for bootstrap CI")

    rng = random.Random(seed)
    deltas: list[float] = []

    for _ in range(samples):
        # Resample indices with replacement
        idx = [rng.randrange(n) for _ in range(n)]
        b_pass = sum(1 for i in idx if bucker_passed[i])
        bl_pass = sum(1 for i in idx if baseline_passed[i])
        deltas.append((b_pass - bl_pass) / n)

    deltas.sort()
    alpha = 1.0 - confidence
    lower_idx = int(len(deltas) * (alpha / 2))
    upper_idx = int(len(deltas) * (1 - alpha / 2))

    return BootstrapCI(
        lower=deltas[lower_idx],
        upper=deltas[upper_idx - 1],  # -1 for 0-index
        mean=sum(deltas) / len(deltas),
        confidence=confidence,
        samples=samples,
    )


# --------------------------------------------------------------- analysis ----


def analyze(
    bucker_passed: list[bool],
    baseline_passed: list[bool],
    *,
    bucker_cost_total: float = 0.0,
    baseline_cost_total: float = 0.0,
) -> PairedStats:
    """Run the full statistical analysis on paired results."""
    outcomes = PairedOutcomes.from_results(bucker_passed, baseline_passed)
    mcnemar = mcnemar_test(outcomes)
    bootstrap = bootstrap_delta_ci(bucker_passed, baseline_passed)

    return PairedStats(
        outcomes=outcomes,
        mcnemar=mcnemar,
        bootstrap=bootstrap,
        bucker_cost_total=bucker_cost_total,
        baseline_cost_total=baseline_cost_total,
    )


# ----------------------------------------------------- chi-squared helper ----


def _chi2_sf(x: float, df: int) -> float:
    """Survival function (1 - CDF) for chi-squared distribution.

    Exact closed forms where cheap ones exist:
      * df=1: χ²(1) is the square of a standard normal, so
        P(χ² > x) = erfc(√(x/2)).
      * df=2: χ²(2) is Exponential(1/2), so P(χ² > x) = e^{-x/2}.
      * df=4: P(χ² > x) = e^{-x/2}(1 + x/2).
      * any even df: P(χ²_{2m} > x) = e^{-x/2} Σ_{j=0}^{m-1} (x/2)^j / j!.

    These are exact and cannot accumulate error; the regularised incomplete
    gamma (below) is only reached for odd df > 1, which this module never
    uses today (McNemar is always df=1). It is kept correct and tested so
    the trap does not lie in wait for the next person.
    """
    if x < 0:
        return 1.0
    if x == 0:
        return 1.0
    if df == 1:
        return math.erfc(math.sqrt(x / 2.0))
    if df == 2:
        return math.exp(-x / 2.0)
    if df == 4:
        return math.exp(-x / 2.0) * (1.0 + x / 2.0)
    if df % 2 == 0:
        half = x / 2.0
        total = 0.0
        term = 1.0
        for j in range(df // 2):
            total += term
            term *= half / (j + 1)
        return math.exp(-half) * total
    # Odd df > 1: regularised incomplete gamma. Implemented here so the
    # stats module has zero external dependencies.
    return 1.0 - _gammainc(df / 2.0, x / 2.0)


def _gammainc(a: float, x: float, terms: int = 200) -> float:
    """Regularised lower incomplete gamma function P(a, x).

    Series expansion (DLMF 8.7.3):

        P(a, x) = (x^a e^{-x} / Γ(a)) · Σ_{n=0}^∞ x^n / (a(a+1)···(a+n))

    The denominator is the key detail: it starts at ``a`` itself, not at
    ``a + 1``. Omitting that leading factor silently multiplies the whole
    sum by ``a`` — harmless for a = 1, wrong by a constant factor for every
    other shape, and it makes chi-squared p-values for df > 2 go negative.
    (Caught by the df=4/df=6 fixtures in test_stats.py.)

    Converges rapidly for the chi-squared use case (a = 0.5, x = χ²/2 with
    χ² typically < 10). For production use with extreme values, consider
    scipy.stats.chi2.sf.
    """
    if x < 0:
        return 0.0
    if x == 0:
        return 0.0
    if a <= 0:
        raise ValueError(f"shape parameter a must be positive, got {a}")

    # Normalisation: 1 / Γ(a)
    log_gamma_a = math.lgamma(a)

    total = 0.0
    for n in range(terms):
        factor = 1.0
        for k in range(n + 1):
            factor *= (a + k)
        term = (x ** n) / factor
        total += term
        if term < 1e-15 * (total + 1e-30):
            break

    return total * (x ** a) * math.exp(-x) / math.exp(log_gamma_a)
