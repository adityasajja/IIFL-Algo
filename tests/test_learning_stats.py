"""The learning engine's statistics, checked against values that are known.

These tests are written the way the module claims its numbers should be read:
against an externally verifiable answer, and against the *failure* cases. A
statistics module that only gets tested on well-behaved data is a module whose
edge cases are discovered in production, where the edge case is a strategy
decision.

The Wilson interval values below are the published ones for the worked examples
in the literature, so they test the implementation rather than restating it.
"""

from __future__ import annotations

import math

import pytest

from atr.research import learning_stats as stats


# ---------------------------------------------------------------------------
# the honesty floor
# ---------------------------------------------------------------------------


def test_min_sample_is_high_enough_to_be_meaningful():
    """A guard on the guard: a floor of 2 would make every test below vacuous."""
    assert stats.MIN_SAMPLE >= 10
    assert stats.MIN_SAMPLE < stats.SMALL_SAMPLE


def test_wilson_matches_the_published_interval():
    """18/30 — the textbook case. Roughly 42%–75%, which spans a coin flip."""
    low, high = stats.wilson_interval(18, 30)
    assert low == pytest.approx(0.4232, abs=5e-4)
    assert high == pytest.approx(0.7541, abs=5e-4)
    # The point of quoting it: a 60% win rate on 30 trades cannot be
    # distinguished from a coin flip at 95% confidence. If this assertion ever
    # fails because the interval narrowed, the sample grew — not something to
    # "fix".
    assert low < 0.5 < high


def test_wilson_stays_inside_the_unit_interval_at_the_edges():
    """Wald would return 0.75 ± 0.42 here — an upper bound above 100%."""
    low, high = stats.wilson_interval(3, 4)
    assert 0.0 <= low < high <= 1.0
    assert high < 1.0


def test_wilson_on_an_empty_sample_admits_everything():
    """No observations means every rate is possible — not a rate of zero."""
    assert stats.wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        stats.wilson_interval(11, 10)


def test_wilson_narrows_as_the_sample_grows():
    narrow = stats.wilson_interval(600, 1000)
    wide = stats.wilson_interval(6, 10)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


# ---------------------------------------------------------------------------
# summaries refuse to overstate
# ---------------------------------------------------------------------------


def test_profit_factor_is_undefined_without_losses():
    """No losing trade means an infinite PF. None says so; a sentinel lies."""
    assert stats.profit_factor([1.0, 2.0, 3.0]) is None
    assert stats.profit_factor([2.0, -1.0, 3.0, -1.0]) == pytest.approx(2.5)


def test_mean_interval_uses_the_samples_own_dispersion():
    values = [10.0] * 10 + [-9.0] * 10
    interval = stats.mean_ci(values)
    assert interval is not None
    low, high = interval
    assert low < 0.5 < high


def test_a_zero_variance_sample_has_a_point_interval_not_none():
    """All-identical values give an exact mean. None would read as 'unknown'."""
    assert stats.mean_ci([5.0, 5.0, 5.0]) == (5.0, 5.0)


def test_mean_interval_is_none_for_a_single_observation():
    assert stats.mean_ci([5.0]) is None
    assert stats.mean_ci([]) is None
    assert stats.stdev([5.0]) is None


def test_non_finite_and_missing_values_are_dropped_not_zeroed():
    """A NaN P&L is a missing measurement, not a break-even trade."""
    assert stats.stdev([1.0, float("nan"), None, 3.0]) == pytest.approx(math.sqrt(2.0))
    assert stats.summarise([1.0, float("nan")]).n == 1


def test_without_best_exposes_a_single_trade_carrying_a_bucket():
    values = [100.0] + [-1.0] * 9
    summary = stats.summarise(values)
    assert summary.mean > 0
    assert summary.without_best is not None and summary.without_best < 0
    # The raw mean is +9.1 and the without-best mean is -1.0: the bucket is one
    # trade and eight small losses wearing the mask of a strategy.
    assert summary.without_best < 0 < summary.mean


def test_trimmed_mean_resists_an_outlier_that_moves_the_mean():
    values = [10.0] * 20 + [1000.0]
    assert stats.trimmed_mean(values) < stats.summarise(values).mean


def test_win_rate_counts_strictly_positive():
    wins, total, rate = stats.win_rate_outcomes([1.0, 0.0, -1.0, 2.0])
    assert (wins, total) == (2, 4)
    assert rate == 0.5


# ---------------------------------------------------------------------------
# comparison, and the multiple-testing correction
# ---------------------------------------------------------------------------


def test_bonferroni_is_capped_at_one():
    """A p-value is a probability; reporting 1.5 would be nonsense."""
    assert stats.bonferroni(0.5, 10) == 1.0
    assert stats.bonferroni(0.01, 10) == pytest.approx(0.1)


def test_bonferroni_rejects_a_zero_test_count():
    with pytest.raises(ValueError):
        stats.bonferroni(0.01, 0)


def test_welch_detects_a_clear_separation():
    test = stats.welch_t([10.0, 11.0, 12.0, 13.0], [-10.0, -11.0, -12.0, -13.0])
    assert test is not None
    statistic, dof = test
    assert statistic > 5
    assert dof > 0
    assert stats.normal_two_sided_p(statistic) < 0.001


def test_welch_handles_a_zero_variance_side():
    """One side constant is a normal case, not a degenerate one.

    A fixed 15% stop realises the same percentage on every trade, so a bucket can
    legitimately have zero dispersion. The standard error is then non-zero
    because the other side varies, and the test is perfectly well defined.
    """
    left = [1.0, 1.0, 1.0]
    right = [1.0, 2.0, 3.0]
    test = stats.welch_t(left, right)
    assert test is not None
    statistic, dof = test
    assert statistic < 0  # left is below right
    assert dof > 0


def test_welch_is_infinite_when_both_sides_are_constant():
    """Both constant and different: the separation is exact, so t is unbounded.

    Returned as a large finite value rather than infinity so the p-value stays a
    number the caller can compare. ``None`` here would read as "no evidence" for
    what is the strongest separation the data admits.
    """
    statistic, dof = stats.welch_t([5.0] * 12, [1.0] * 12)
    assert statistic > 100  # left is well above right
    assert dof > 0
    assert stats.normal_two_sided_p(statistic) < 1e-6


def test_p_value_of_zero_deviation_is_one():
    assert stats.normal_two_sided_p(0.0) == pytest.approx(1.0)


def test_significance_labels_are_words_not_stars():
    assert stats.significance_label(0.005) == "strong"
    assert stats.significance_label(0.03) == "moderate"
    assert stats.significance_label(0.08) == "weak"
    assert stats.significance_label(0.5) == "not_significant"
    assert stats.significance_label(None) == "not_tested"


# ---------------------------------------------------------------------------
# bucket comparison
# ---------------------------------------------------------------------------


def test_a_small_bucket_is_suppressed_not_reported_with_a_caveat():
    verdict = stats.compare_bucket("tiny", [1.0, 2.0, 3.0], [0.0] * 40)
    assert verdict.suppressed is True
    assert verdict.lift is None
    assert verdict.p_value is None
    assert verdict.significance == "insufficient_sample"
    assert "floor" in (verdict.note or "")
    # The count is still reported: suppressing the analysis is not hiding it.
    assert verdict.n == 3


def test_a_sufficient_bucket_is_measured_against_its_complement():
    bucket = [10.0, 11.0, 12.0, 10.5, 11.5, 10.2, 11.8, 10.9, 11.1, 10.4, 11.6, 10.7]
    baseline = [-1.0] * 30
    verdict = stats.compare_bucket("good", bucket, baseline, comparisons=1)
    assert verdict.suppressed is False
    assert verdict.lift is not None and verdict.lift > 10
    assert verdict.significance == "strong"


def test_the_correction_is_applied_against_the_declared_bucket_count():
    """A bucket one point above the baseline, compared against 1 or 50 buckets.

    The dispersion is deliberately wide so the raw p-value is neither 0 nor 1 —
    with a stark separation the correction is invisible because p is already
    near zero, and the test would pass without exercising anything.
    """
    bucket = [1.5, 2.5, 1.8, 2.2, 1.6, 2.4, 1.9, 2.1, 1.7, 2.3, 2.0, 1.4]
    baseline = [1.0, 3.0, 0.8, 3.2, 1.2, 2.8, 1.1, 2.9, 0.9, 3.1, 1.3, 2.7]
    single = stats.compare_bucket("x", bucket, baseline, comparisons=1)
    many = stats.compare_bucket("x", bucket, baseline, comparisons=50)

    assert single.p_value is not None
    assert single.p_value == many.p_value  # the raw test is unchanged
    assert many.p_adjusted == pytest.approx(min(1.0, single.p_value * 50))
    assert many.p_adjusted >= single.p_adjusted
    assert many.comparisons == 50


def test_a_bucket_identical_to_its_baseline_tests_as_no_difference():
    statistic, _ = stats.welch_t([2.0] * 12, [2.0] * 12)
    assert statistic == 0.0
    assert stats.normal_two_sided_p(statistic) == pytest.approx(1.0)


def test_welch_is_none_when_either_sample_is_too_small():
    assert stats.welch_t([1.0], [2.0, 3.0, 4.0]) is None
    assert stats.welch_t([2.0, 3.0, 4.0], []) is None


def test_a_bucket_needing_one_trade_is_flagged_but_not_discarded():
    """Fat tails are real. The flag says 'check', not 'delete'."""
    bucket = [500.0] + [-5.0] * 11
    verdict = stats.compare_bucket("lottery", bucket, [-5.0] * 30, comparisons=1)
    assert verdict.suppressed is False
    assert verdict.note is not None
    assert "single best trade" in verdict.note


def test_ranking_puts_wide_samples_above_lucky_small_ones():
    """Sorting by lift alone would rank the 3-trade bucket first. It must not."""
    good = stats.compare_bucket(
        "wide", [5.0] * 40, [0.0] * 40, comparisons=1
    )
    lucky = stats.compare_bucket("narrow", [8.0, 8.0, -20.0], [0.0] * 40, comparisons=1)
    ranked = stats.rank_buckets([lucky, good], require_significant=False)
    assert ranked[0].label == "wide"


def test_ranking_can_require_significance():
    """An insignificant bucket is dropped; the filter must not be a no-op."""
    # A tight bucket and a tight baseline one point apart: significant.
    significant = stats.compare_bucket(
        "tight_apart",
        [1.5, 1.51, 1.49, 1.52, 1.48, 1.5, 1.5, 1.51, 1.49, 1.5, 1.5, 1.51],
        [1.0, 1.01, 0.99, 1.02, 0.98, 1.0, 1.0, 1.01, 0.99, 1.0, 1.0, 1.01],
        comparisons=1,
    )
    # The same two values observed with the same dispersion — no difference.
    insignificant = stats.compare_bucket("identical", [1.0] * 12, [1.0] * 12, comparisons=1)

    assert significant.significance in {"strong", "moderate", "weak"}
    assert insignificant.significance == "not_significant"

    kept = stats.rank_buckets([significant, insignificant], require_significant=True)
    assert [v.label for v in kept] == ["tight_apart"]
    both = stats.rank_buckets([significant, insignificant], require_significant=False)
    assert {v.label for v in both} == {"tight_apart", "identical"}


# ---------------------------------------------------------------------------
# purity
# ---------------------------------------------------------------------------


def test_the_statistics_module_imports_nothing_from_atr():
    """A statistic that depends on a trade table is not reproducible.

    Enforced here rather than by review, because the obvious way to make this
    module useful later is to let it read a repository — and the moment it does,
    it stops being testable against a known answer.
    """
    import ast
    import pathlib

    path = pathlib.Path(stats.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("atr"):
            offenders.append(f"from {node.module}")
        elif isinstance(node, ast.Import):
            offenders.extend(
                f"import {alias.name}" for alias in node.names if alias.name.startswith("atr")
            )
    assert offenders == [], f"learning_stats must stay dependency-free: {offenders}"


def test_the_statistics_module_imports_nothing_heavy():
    """Pure stdlib: no numpy, no pandas.

    Not dogma — a bootstrap or a vectorised t-test would be a reasonable thing
    to add here, and this test is the prompt to make that an explicit decision
    (and to revise this test) rather than an accidental import.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(stats.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"numpy", "pandas", "scipy"}), imported
