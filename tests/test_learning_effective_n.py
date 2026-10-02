"""Effective sample size under clustered dependence.

On 2026-09-28 fourteen paper trades closed inside one market minute and the
engine banked findings at n=14 with middling confidence. Fourteen readings of
the same minute are one independent observation, not fourteen — the confidence
was counting rows instead of evidence. These tests pin the clustering so a
single market moment can never mint a finding again.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from atr.research.learning_stats import compare_bucket, effective_n


def _minute(offset: int = 0) -> datetime:
    return datetime(2026, 9, 28, 4, 15) + timedelta(seconds=offset)


def test_fourteen_trades_in_one_minute_count_as_one() -> None:
    """The morning that motivated this: one cluster, effective size one."""
    stamps = [_minute(s) for s in range(14)]
    n_eff, clusters = effective_n(stamps)
    assert clusters == 1
    assert n_eff == 1.0


def test_spread_out_trades_keep_full_credit() -> None:
    """No clustering detected, no penalty applied."""
    stamps = [datetime(2026, 9, d, 9, 30) for d in range(1, 15)]
    n_eff, clusters = effective_n(stamps)
    assert clusters == 14
    assert n_eff == 14.0


def test_two_clusters_split_credit_by_kish() -> None:
    """Ten trades in one minute and four spread out: 14^2 / (100 + 4)."""
    stamps = [_minute(s) for s in range(10)]
    stamps += [datetime(2026, 9, d, 9, 30) for d in range(2, 6)]
    n_eff, clusters = effective_n(stamps)
    assert clusters == 5
    assert abs(n_eff - (14 * 14 / (100 + 4))) < 1e-9


def test_unparseable_stamps_are_not_penalised() -> None:
    """No timing evidence means no dependence claim: own cluster each."""
    n_eff, clusters = effective_n([None, "not-a-date", _minute(0)])
    assert (n_eff, clusters) == (3.0, 3)


def test_empty_sample_is_zero_not_an_error() -> None:
    assert effective_n([]) == (0.0, 0)


def test_iso_strings_cluster_like_datetimes() -> None:
    stamps = [(_minute(s)).isoformat() for s in range(5)]
    n_eff, clusters = effective_n(stamps)
    assert (n_eff, clusters) == (1.0, 1)


def test_clustered_bucket_is_suppressed_with_the_reason_stated() -> None:
    """Same fourteen values that banked a finding yesterday are withheld today."""
    values = [-300.0] * 14
    others = [50.0] * 30
    stamps = [_minute(s) for s in range(14)]
    verdict = compare_bucket(
        "cost_drowned", values, others, bucket_times=stamps, min_sample=10
    )
    assert verdict.suppressed
    assert verdict.n == 14
    assert verdict.n_effective == 1.0
    assert verdict.clusters == 1
    assert "1 time cluster" in (verdict.note or "")


def test_absent_timestamps_change_nothing() -> None:
    """Back-compat: callers that pass no stamps get exactly the old behaviour."""
    values = [-300.0] * 14
    others = [50.0] * 30
    verdict = compare_bucket("cost_drowned", values, others, min_sample=10)
    assert not verdict.suppressed
    assert verdict.n_effective == 14.0
    assert verdict.clusters == 14
