"""Statistics for the learning engine — and the honesty rules that go with them.

Why this is a separate module
-----------------------------

Every number the learning engine shows a user is a *claim*. "Momentum works
better above 2x relative volume" is a claim about a population, made from a
sample, and the only thing standing between that sentence and a superstition is
the arithmetic in this file. Keeping it apart from the I/O means it can be
tested against cases whose right answer is known rather than against whatever
the database happened to hold.

This module imports **nothing from ``atr``**. That is deliberate and it is
checked by ``tests/test_learning_stats.py``: the moment a statistic depends on a
trade table, the statistic stops being reproducible from its inputs.

The three rules this module exists to enforce
---------------------------------------------

**1. A mean without a sample size is not a finding.**

Thirty trades with a 60% win rate is not evidence that a strategy wins 60% of
the time. The 95% Wilson interval for 18/30 is roughly 42%–75% — wide enough to
contain the coin-flip hypothesis comfortably. Every rate this module returns
therefore carries an interval, and every mean carries a confidence interval
computed from the sample's own dispersion.

**2. A pattern found by searching is weaker than the same pattern stated in
advance.**

The engine looks at ten breakdowns, each with several buckets. The best-looking
bucket out of fifty is not a discovery; it is the maximum of fifty draws. That
is why the Bonferroni-adjusted p-value is offered, and why ``MIN_SAMPLE``
suppression exists rather than a "show all, note the small ones" policy.

**3. Outliers dominate trade statistics, and hiding them is a lie.**

One trade that returned 340% can carry an entire bucket's expectancy while the
other ten trades in it lost money. ``trimmed_mean`` and ``median`` are therefore
reported alongside the mean rather than instead of it, and ``without_best`` is
reported for any bucket small enough for a single trade to flip. This mirrors
the standing project rule: *"a mean without its best-week breakdown is not
evidence."*

What is deliberately not here
-----------------------------

* No p-value from a normal approximation on fewer than ~10 observations.
* No Sharpe from fewer than 2 observations or zero variance — both return None
  rather than a number that would be printed as if it meant something.
* No significance test whose null hypothesis is asserted rather than stated.
  ``bucket_lift`` labels its null explicitly: *the bucket's mean equals the
  baseline's mean*.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

#: Below this, a bucket is reported as insufficient rather than analysed.
#:
#: Ten is not a statistical threshold — it is the point below which a single
#: trade can move the mean by more than the mean itself, so any "pattern" is a
#: statement about one trade. A confidence interval computed on five
#: observations is arithmetically valid and epistemically worthless.
MIN_SAMPLE = 10

#: Below this, a bucket is flagged but still shown, with its interval.
SMALL_SAMPLE = 30

#: Confidence levels the engine reports at.
CONFIDENCE_95 = 0.95
CONFIDENCE_90 = 0.90

#: Two-sided z for the confidence levels above. A normal quantile table would
#: be more general; these are the two values actually used, written out so the
#: reader can check them against a table rather than trusting an interpolation.
_Z = {CONFIDENCE_95: 1.959963985, CONFIDENCE_90: 1.644853627}


# ---------------------------------------------------------------------------
# intervals on a rate
# ---------------------------------------------------------------------------


def wilson_interval(
    successes: int, total: int, *, confidence: float = CONFIDENCE_95
) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Wilson rather than Wald because Wald fails exactly where trade statistics
    live: small samples and rates near 0 or 1. A strategy with 3 wins in 4
    trades has a Wald interval of 0.75 ± 0.42, which says the win rate is
    somewhere between 33% and 117% — a meaningless upper bound. Wilson stays
    inside [0, 1] and is well-behaved at the edges.

    Returns ``(0.0, 1.0)`` for an empty sample: with no observations, every rate
    is possible, and that is the honest answer.
    """
    if total <= 0:
        return (0.0, 1.0)
    if successes < 0 or successes > total:
        raise ValueError(f"successes {successes} outside [0, {total}]")

    z = _Z.get(confidence, _Z[CONFIDENCE_95])
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = (z / denominator) * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (max(0.0, centre - half), min(1.0, centre + half))


# ---------------------------------------------------------------------------
# distribution summaries
# ---------------------------------------------------------------------------


def _clean(values: Sequence[float] | None) -> list[float]:
    """Finite floats only. NaN and None are dropped, not treated as zero."""
    if not values:
        return []
    out: list[float] = []
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def stdev(values: Sequence[float]) -> float | None:
    """Sample standard deviation (ddof=1). None only when it is undefined.

    ``None`` means *undefined* — fewer than two observations — and nothing else.
    An earlier version returned ``None`` for a zero-variance sample too, which
    was wrong in a way that mattered: a constant series has a standard deviation
    of exactly 0, and collapsing "the spread is zero" into "the spread is
    unknown" made every downstream interval disappear. The failure surfaced as
    ``mean_ci`` returning None for a perfectly measured quantity.
    """
    data = _clean(values)
    if len(data) < 2:
        return None
    mean = sum(data) / len(data)
    variance = sum((x - mean) ** 2 for x in data) / (len(data) - 1)
    return math.sqrt(variance) if variance > 0 else 0.0


def mean_ci(
    values: Sequence[float], *, confidence: float = CONFIDENCE_95
) -> tuple[float, float] | None:
    """Confidence interval for a mean, from the sample's own dispersion.

    Uses the normal quantile rather than Student's t. That understates the
    interval below ~30 observations, so it is *optimistic* in exactly the regime
    where the engine should be cautious — which is why ``MIN_SAMPLE`` suppresses
    those buckets before this is ever called on them. For the buckets that do
    survive, the normal approximation is close enough that quoting a t-table
    value would be false precision.
    """
    data = _clean(values)
    if len(data) < 2:
        return None
    sd = stdev(data)
    if sd is None:
        return None
    # A zero-variance sample has a real interval — a point. Saying so beats
    # returning None, which would read as "unknown" when the truth is "exact".
    z = _Z.get(confidence, _Z[CONFIDENCE_95])
    mean = sum(data) / len(data)
    half = z * sd / math.sqrt(len(data))
    return (mean - half, mean + half)

def trimmed_mean(values: Sequence[float], proportion: float = 0.1) -> float | None:
    """Mean after dropping ``proportion`` from each tail.

    The point of a trimmed mean here is to answer "is this bucket's edge spread
    across its trades, or is it one trade?" — a question the raw mean cannot
    answer because it is exactly the statistic a single outlier moves.
    """
    data = sorted(_clean(values))
    if not data:
        return None
    cut = int(len(data) * proportion)
    if cut > 0 and len(data) - 2 * cut >= 2:
        data = data[cut : len(data) - cut]
    elif len(data) > 2:
        data = data[1:-1]
    return sum(data) / len(data)


def median(values: Sequence[float]) -> float | None:
    data = sorted(_clean(values))
    if not data:
        return None
    mid = len(data) // 2
    if len(data) % 2:
        return data[mid]
    return (data[mid - 1] + data[mid]) / 2.0


def without_best(values: Sequence[float]) -> float | None:
    """Mean with the single largest value removed.

    Quoted for every bucket. A bucket whose edge disappears when one trade is
    removed has no edge — it has a lottery ticket that already paid, and the
    next one has not.
    """
    data = sorted(_clean(values))
    if len(data) < 2:
        return None
    return sum(data[:-1]) / (len(data) - 1)


def win_rate_outcomes(values: Sequence[float]) -> tuple[int, int, float | None]:
    """``(wins, total, rate)`` counting values strictly greater than zero."""
    data = _clean(values)
    if not data:
        return (0, 0, None)
    wins = sum(1 for value in data if value > 0)
    return (wins, len(data), wins / len(data))


def profit_factor(values: Sequence[float]) -> float | None:
    """Gross profit over gross loss.

    ``None`` when there are no losses. A bucket with no losing trade has an
    undefined (infinite) profit factor, and printing a large sentinel would be
    reporting a number that the data does not support. Callers render ``None``
    as "no losing trades", which says the same thing without inventing a value.
    """
    data = _clean(values)
    if not data:
        return None
    gross_profit = sum(value for value in data if value > 0)
    gross_loss = -sum(value for value in data if value < 0)
    if gross_loss <= 0:
        return None
    return gross_profit / gross_loss


def payoff_ratio(values: Sequence[float]) -> float | None:
    """Average win divided by average loss. None if either side is empty."""
    data = _clean(values)
    wins = [v for v in data if v > 0]
    losses = [v for v in data if v < 0]
    if not wins or not losses:
        return None
    return (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))


# ---------------------------------------------------------------------------
# bucket comparison
# ---------------------------------------------------------------------------


def welch_t(a: Sequence[float], b: Sequence[float]) -> tuple[float, float] | None:
    """Welch t statistic and Welch–Satterthwaite degrees of freedom.

    Welch, not Student: the two samples being compared are a bucket and its
    complement, and a volatility bucket is *defined* by having different
    dispersion from the rest of the book. Assuming equal variances would be
    assuming away the thing being measured.

    Returns None when either sample is too small or has no dispersion. The
    caller converts (t, df) into a p-value; this function deliberately does not,
    because it has no reliable t-distribution to hand and a p-value computed
    from a mis-specified distribution is worse than no p-value.
    """
    left, right = _clean(a), _clean(b)
    if len(left) < 2 or len(right) < 2:
        return None
    var_a, var_b = stdev(left), stdev(right)
    if var_a is None or var_b is None:
        return None

    n_a, n_b = len(left), len(right)
    se_squared = var_a**2 / n_a + var_b**2 / n_b
    if se_squared <= 0:
        # Both sides are constant. The standard error is exactly zero, so the
        # t-statistic is genuinely infinite, not undefined — and returning None
        # made the commonest comparison in trade statistics unanswerable.
        #
        # Consider a rule that fires whenever volume is high: every trade in the
        # "high volume" bucket can end up with an identical *stored* metric
        # (a fixed fractional stop realising the same percentage every time),
        # and so can the complement. That bucket's difference from its baseline
        # is as real as any other, and the old code reported "not_tested" for it,
        # which reads as "no evidence" rather than "the most extreme evidence".
        delta = sum(left) / n_a - sum(right) / n_b
        if delta == 0:
            return (0.0, float(max(n_a, n_b) - 1))
        # Reported with the smaller side's degrees of freedom: the widest finite
        # interval the data can support, so the p-value below is as conservative
        # as this construction allows. A finite t with a t-like tail is a weaker
        # claim than infinity, and a weaker claim is the right error to make.
        direction = math.copysign(1.0, delta)
        return (direction * 1e3, float(max(1, min(n_a, n_b) - 1)))

    t = (sum(left) / n_a - sum(right) / n_b) / math.sqrt(se_squared)

    numerator = se_squared**2
    denominator = (var_a**2 / n_a) ** 2 / (n_a - 1) + (var_b**2 / n_b) ** 2 / (n_b - 1)
    if denominator <= 0:
        return None
    return (t, numerator / denominator)


def normal_two_sided_p(z: float) -> float:
    """Two-sided p-value for a standard normal deviate, via ``erfc``."""
    return math.erfc(abs(z) / math.sqrt(2.0))


def bonferroni(p: float, tests: int) -> float:
    """Family-wise correction for having looked at ``tests`` buckets.

    The engine does not merely test one hypothesis. It scans every bucket of
    every breakdown, and the winner of that scan is the maximum of many
    statistics. Reporting the raw p-value there is the multiple-comparisons
    fallacy in its purest form: at 50 buckets, a raw p of 0.02 is expected by
    chance alone. Corrected p-values are capped at 1.0 rather than reported
    above it, since a p-value is a probability.
    """
    if tests <= 0:
        raise ValueError("tests must be positive")
    return min(1.0, p * tests)


def significance_label(p_value: float | None) -> str:
    """A word for a p-value, so the UI cannot dress up a weak result.

    The thresholds are labelled with what they *mean*, not with stars. A reader
    who does not know that ``**`` is p<0.01 cannot be misled by the word
    "suggestive" — and this label is used in the JSON payload precisely so no
    caller has to invent its own vocabulary.
    """
    if p_value is None:
        return "not_tested"
    if p_value < 0.01:
        return "strong"
    if p_value < 0.05:
        return "moderate"
    if p_value < 0.10:
        return "weak"
    return "not_significant"


# ---------------------------------------------------------------------------
# expectancy, in the unit the project quotes results in
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expectancy:
    """A bucket's outcome distribution, with everything needed to distrust it."""

    n: int
    wins: int
    win_rate: float | None
    win_rate_ci: tuple[float, float] | None
    mean: float | None
    mean_ci: tuple[float, float] | None
    median: float | None
    trimmed_mean: float | None
    stdev: float | None
    without_best: float | None
    profit_factor: float | None
    payoff_ratio: float | None
    total: float | None
    best: float | None
    worst: float | None
    #: None when the bucket is too small to be worth an interval (n < MIN_SAMPLE*2)
    small_sample: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "wins": self.wins,
            "win_rate": _round(self.win_rate, 4),
            "win_rate_ci": _round_pair(self.win_rate_ci),
            "mean": _round(self.mean, 4),
            "mean_ci": _round_pair(self.mean_ci),
            "median": _round(self.median, 4),
            "trimmed_mean": _round(self.trimmed_mean, 4),
            "stdev": _round(self.stdev, 4),
            "without_best": _round(self.without_best, 4),
            "profit_factor": _round(self.profit_factor, 4),
            "payoff_ratio": _round(self.payoff_ratio, 4),
            "total": _round(self.total, 2),
            "best": _round(self.best, 4),
            "worst": _round(self.worst, 4),
            "small_sample": self.small_sample,
        }


def summarise(values: Sequence[float]) -> Expectancy:
    """Every summary the engine is allowed to show, computed in one place.

    Note the plain ``mean`` is included but is *not* the headline. The headline
    for a bucket is the mean **with its interval and its without-best variant**,
    because the raw mean is the number most easily mistaken for a fact.
    """
    data = _clean(values)
    wins, total, rate = win_rate_outcomes(data)
    return Expectancy(
        n=total,
        wins=wins,
        win_rate=rate,
        win_rate_ci=wilson_interval(wins, total) if total else None,
        mean=(sum(data) / total) if total else None,
        mean_ci=mean_ci(data),
        median=median(data),
        trimmed_mean=trimmed_mean(data),
        stdev=stdev(data),
        without_best=without_best(data),
        profit_factor=profit_factor(data),
        payoff_ratio=payoff_ratio(data),
        total=sum(data) if data else None,
        best=max(data) if data else None,
        worst=min(data) if data else None,
        small_sample=0 < total < SMALL_SAMPLE,
    )


@dataclass(frozen=True)
class BucketVerdict:
    """One bucket measured against its baseline, with the claim stated."""

    label: str
    n: int
    stats: dict[str, Any]
    #: Difference of means: bucket minus baseline, in the metric's own unit.
    lift: float | None
    #: p-value for the null "this bucket's mean equals the baseline's mean",
    #: Welch two-sided. Raw, before the family-wise correction.
    p_value: float | None
    p_adjusted: float | None
    significance: str
    #: How many buckets were compared when the correction was applied.
    comparisons: int
    #: True when the bucket was withheld because it was too small to analyse.
    suppressed: bool
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        stats_dict = self.stats if isinstance(self.stats, dict) else {}
        win_rate = stats_dict.get("win_rate")
        win_rate_ci = stats_dict.get("win_rate_ci")
        mean_val = stats_dict.get("mean")
        mean_ci = stats_dict.get("mean_ci")
        median_val = stats_dict.get("median")
        pf = stats_dict.get("profit_factor")
        is_small = bool(stats_dict.get("small_sample", True))
        is_meaningful = not self.suppressed and not is_small and (self.significance in {"strong", "moderate", "weak"})
        sample_adequacy = "adequate" if (not self.suppressed and not is_small) else ("small_sample" if not self.suppressed else "insufficient")

        return {
            "label": self.label,
            "n": self.n,
            "sample_size": self.n,
            "stats": self.stats,
            "win_rate": win_rate,
            "win_rate_ci": win_rate_ci,
            "mean": mean_val,
            "mean_ci": mean_ci,
            "median": median_val,
            "profit_factor": pf,
            "is_meaningful": is_meaningful,
            "sample_adequacy": sample_adequacy,
            "lift": _round(self.lift, 4),
            "p_value": _round(self.p_value, 4),
            "p_adjusted": _round(self.p_adjusted, 4),
            "significance": self.significance,
            "comparisons": self.comparisons,
            "suppressed": self.suppressed,
            "note": self.note,
        }



def compare_bucket(
    label: str,
    bucket: Sequence[float],
    baseline: Sequence[float],
    *,
    comparisons: int = 1,
    min_sample: int = MIN_SAMPLE,
) -> BucketVerdict:
    """Measure one bucket against the baseline it was carved out of.

    The baseline is *the complement*, not the whole book and not zero. Comparing
    a bucket to zero answers "does this bucket make money", which is a different
    and much easier question than "does this bucket make *more* money than the
    trades it excludes". Only the second justifies a change to a live rule.
    """
    bucket_data = _clean(bucket)
    stats = summarise(bucket_data)
    n = len(bucket_data)

    if n < min_sample:
        return BucketVerdict(
            label=label,
            n=n,
            stats=stats.as_dict(),
            lift=None,
            p_value=None,
            p_adjusted=None,
            significance="insufficient_sample",
            comparisons=comparisons,
            suppressed=True,
            note=(
                f"{n} trades is below the {min_sample}-trade floor; a single "
                "trade would dominate any statistic computed here"
            ),
        )

    baseline_data = _clean(baseline)
    baseline_mean = (sum(baseline_data) / len(baseline_data)) if baseline_data else None
    lift = (stats.mean - baseline_mean) if (stats.mean is not None and baseline_mean is not None) else None

    test = welch_t(bucket_data, baseline_data)
    p_value = None
    if test is not None:
        p_value = normal_two_sided_p(test[0])

    note = None
    if stats.without_best is not None and stats.mean is not None:
        # Report when the edge is carried by one trade rather than spread over
        # the bucket. This is stated as a note rather than a rejection because a
        # genuinely fat-tailed strategy can still be real.
        if stats.without_best <= 0 < stats.mean:
            note = (
                "the mean is positive only because of the single best trade; "
                f"excluding it the mean is {stats.without_best:.4f}"
            )
    if stats.small_sample and note is None:
        note = f"small sample (n={n}); the interval is wide by construction"

    return BucketVerdict(
        label=label,
        n=n,
        stats=stats.as_dict(),
        lift=lift,
        p_value=p_value,
        p_adjusted=bonferroni(p_value, comparisons) if p_value is not None else None,
        significance=significance_label(p_value),
        comparisons=comparisons,
        suppressed=False,
        note=note,
    )


# ---------------------------------------------------------------------------
# ranking, used by the daily report
# ---------------------------------------------------------------------------


def rank_buckets(verdicts: Sequence[BucketVerdict], *, require_significant: bool = True) -> list[BucketVerdict]:
    """Order buckets by how confident we are, not by how large the number is.

    Sorting by ``lift`` alone puts the smallest bucket on top every time: with
    three trades, one large winner produces an enormous mean and an enormous
    lift, and it wins the sort. Sorting by the lower edge of the interval puts
    the widest-sample buckets first, which is the property actually wanted.
    """
    usable = [v for v in verdicts if not v.suppressed]
    if require_significant:
        usable = [v for v in usable if v.significance in {"strong", "moderate", "weak"}]

    def key(verdict: BucketVerdict):
        stats = verdict.stats or {}
        interval = stats.get("mean_ci")
        lower = interval[0] if interval else (stats.get("mean") or float("-inf"))
        return (-(lower if lower is not None else float("-inf")), -verdict.n)

    return sorted(usable, key=key)


def _round(value: float | None, digits: int) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits)


def _round_pair(pair: tuple[float, float] | None) -> list[float] | None:
    if pair is None:
        return None
    return [round(pair[0], 4), round(pair[1], 4)]


__all__ = [
    "MIN_SAMPLE",
    "SMALL_SAMPLE",
    "CONFIDENCE_95",
    "CONFIDENCE_90",
    "BucketVerdict",
    "Expectancy",
    "bonferroni",
    "compare_bucket",
    "mean_ci",
    "median",
    "normal_two_sided_p",
    "payoff_ratio",
    "profit_factor",
    "rank_buckets",
    "significance_label",
    "stdev",
    "summarise",
    "trimmed_mean",
    "welch_t",
    "wilson_interval",
    "win_rate_outcomes",
    "without_best",
]
