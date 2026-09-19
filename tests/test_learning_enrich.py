"""Entry-time features, and the proof that they cannot see the future.

The look-ahead tests are the reason this file exists. A feature computed from
bars after the entry makes every statistic downstream look better than reality
and — uniquely among bugs — leaves no trace in the output, because the
contaminated number is a perfectly plausible number.

So the frames here are built with a past that is flat and a future that is not.
A 60-bar run at 100 followed by 30 bars at 500. Any leak shows up as an
absurd value rather than a subtle one: a close of 500 for a trade entered at
100, a relative volume of 90x, an ATR of 100%. Those are unmistakable.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from atr.research import learning_enrich as enrich


def _split_frame() -> tuple[pd.DataFrame, pd.Timestamp]:
    """Flat past at 100, explosive future at 500. Returns the frame and the cut."""
    past = pd.DataFrame(
        {
            "ts": pd.date_range("2026-01-01", periods=60, freq="B"),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1_000.0,
        }
    )
    future = pd.DataFrame(
        {
            "ts": pd.date_range("2026-03-30", periods=30, freq="B"),
            "open": 500.0,
            "high": 600.0,
            "low": 450.0,
            "close": 500.0,
            "volume": 90_000.0,
        }
    )
    return pd.concat([past, future], ignore_index=True), past["ts"].iloc[-1]


# ---------------------------------------------------------------------------
# look-ahead — the point of the module
# ---------------------------------------------------------------------------


def test_the_entry_bar_itself_is_visible():
    """The bar at the entry timestamp is usable. Excluding it would drop the
    volume and gap that the rule actually fired on."""
    frame, cut = _split_frame()
    context, _ = enrich.metrics_at(frame, cut)
    assert context.as_of_ts == cut
    assert context.close == 100.0


def test_a_future_price_cannot_leak_into_the_entry_context():
    frame, cut = _split_frame()
    context, _ = enrich.metrics_at(frame, cut)
    assert context.close == 100.0
    assert context.close != 500.0
    assert context.bars_available == 60


def test_a_future_volume_spike_cannot_leak_into_relative_volume():
    """The future is 90x the past volume. A leak would be enormous."""
    frame, cut = _split_frame()
    context, _ = enrich.metrics_at(frame, cut)
    assert context.volume_multiple == pytest.approx(1.0)
    assert context.volume_multiple < 2.0


def test_a_future_price_range_cannot_leak_into_volatility():
    """The future's true range is 150 on a 500 close — 30%. The past is 2%."""
    frame, cut = _split_frame()
    context, _ = enrich.metrics_at(frame, cut)
    assert context.atr_pct == pytest.approx(2.0, abs=0.01)


def test_trend_is_measured_only_against_bars_up_to_the_entry():
    frame, cut = _split_frame()
    assert enrich.trend_at(frame, cut) == pytest.approx(0.0)
    # Measured at the end of the frame it is +47% — the last 50 bars are 30 at
    # 500 plus 20 at 100, so close/SMA50 is 500/340. The point is that the value
    # changes once the cut moves, proving the future really is present in the
    # frame and that the cut is what excludes it.
    at_end = enrich.trend_at(frame, frame["ts"].iloc[-1])
    assert at_end is not None and at_end > 40


def test_moving_the_cut_forward_changes_the_features():
    """A sanity check on the guard itself: if the cut were ignored, the two
    calls below would return identical values and every test above would pass
    for the wrong reason."""
    frame, _ = _split_frame()
    early, _ = enrich.metrics_at(frame, frame["ts"].iloc[40])
    late, _ = enrich.metrics_at(frame, frame["ts"].iloc[80])
    assert early.close == 100.0
    assert late.close == 500.0
    assert early.bars_available != late.bars_available


def test_a_timestamp_before_the_first_bar_yields_no_context():
    frame, _ = _split_frame()
    context, missing = enrich.metrics_at(frame, pd.Timestamp("2020-01-01"))
    assert context.bars_available == 0
    assert context.rsi is None
    assert "rsi" in missing.features


def test_an_unsorted_frame_is_sorted_before_truncating():
    """A cache file written out of order would otherwise return the wrong bar."""
    frame, cut = _split_frame()
    shuffled = frame.sample(frac=1.0, random_state=7).reset_index(drop=True)
    context, _ = enrich.metrics_at(shuffled, cut)
    assert context.close == 100.0
    assert context.as_of_ts == cut


def test_naive_timestamps_compare_against_aware_ones():
    """A tz mismatch silently yields an empty frame, which reads as 'no history'
    rather than 'the join failed' — so it is normalised rather than tolerated."""
    frame, cut = _split_frame()
    aware = frame.assign(ts=frame["ts"].dt.tz_localize("Asia/Kolkata"))
    context, missing = enrich.metrics_at(aware, cut)
    assert context.bars_available == 60
    assert context.close == 100.0


# ---------------------------------------------------------------------------
# individual features
# ---------------------------------------------------------------------------


def _flat_frame(periods: int = 30, **overrides) -> pd.DataFrame:
    data = {
        "ts": pd.date_range("2026-01-01", periods=periods, freq="B"),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 1_000.0,
    }
    data.update(overrides)
    return pd.DataFrame(data)


def test_relative_volume_excludes_the_current_bar_from_its_baseline():
    """Matching ``signals.rules._prior_volume``. Including today would make a
    spike dilute itself, so the dataset and the rule would disagree about the
    same trade — 2x that fired the rule reported as 1.6x."""
    frame = _flat_frame(30)
    frame.loc[frame.index[-1], "volume"] = 2_000.0
    context, _ = enrich.metrics_at(frame, frame["ts"].iloc[-1])
    assert context.volume_multiple == pytest.approx(2.0)


def test_gap_is_measured_against_the_immediately_preceding_session():
    frame = _flat_frame(30)
    frame.loc[frame.index[-1], "open"] = 105.0
    context, _ = enrich.metrics_at(frame, frame["ts"].iloc[-1])
    assert context.gap_pct == pytest.approx(5.0)


def test_a_gap_across_a_long_break_in_the_cache_is_not_an_overnight_gap():
    """Otherwise a symbol that stopped trading for a month reports a multi-week
    move as a gap, and every gap bucket is polluted by suspended names."""
    frame = _flat_frame(30)
    frame.loc[frame.index[-1], "ts"] = frame["ts"].iloc[-2] + pd.Timedelta(days=45)
    frame.loc[frame.index[-1], "open"] = 130.0
    context, _ = enrich.metrics_at(frame, frame["ts"].iloc[-1])
    assert context.gap_pct is None


def test_an_indicator_with_too_few_bars_is_absent_not_approximate():
    frame = _flat_frame(periods=10)
    context, missing = enrich.metrics_at(frame, frame["ts"].iloc[-1])
    assert context.rsi is None
    assert context.atr_pct is None
    assert context.sma_slow is None
    assert "rsi" in missing.features
    assert "sma_slow" in missing.features


def test_the_slow_average_reports_above_or_below_honestly():
    rising = _flat_frame(60, close=np.linspace(100.0, 200.0, 60))
    context, _ = enrich.metrics_at(rising, rising["ts"].iloc[-1])
    assert context.above_slow_sma is True

    falling = _flat_frame(60, close=np.linspace(200.0, 100.0, 60))
    context, _ = enrich.metrics_at(falling, falling["ts"].iloc[-1])
    assert context.above_slow_sma is False


def test_an_empty_frame_yields_every_feature_marked_missing():
    context, missing = enrich.metrics_at(pd.DataFrame(), pd.Timestamp("2026-01-01"))
    assert context.bars_available == 0
    assert "rsi" in missing.features
    assert "relative_volume" in missing.features


# ---------------------------------------------------------------------------
# features that do not exist must be declared, not estimated
# ---------------------------------------------------------------------------


def test_vwap_and_vix_are_reported_missing_on_every_call():
    """The one rule the dataset must never break.

    ``atr.strategy.indicators.vwap`` exists and is correct, but it needs
    intraday bars. The cache holds daily bars timestamped 09:15, so a "daily
    VWAP" would be the rule's own arithmetic wearing a misleading name.
    """
    frame = _flat_frame(60)
    _, missing = enrich.metrics_at(frame, frame["ts"].iloc[-1])
    assert missing.features == sorted(
        ["india_vix", "vwap_relationship"]
    ) or {"vwap_relationship", "india_vix"} <= set(missing.features)
    assert "intraday" in missing.reasons["vwap_relationship"]


def test_missing_features_keeps_the_first_reason_for_a_feature():
    """One reason per feature, so the dataset-level explanation is stable."""
    missing = enrich.MissingFeatures()
    missing.add("rsi", "first")
    missing.add("rsi", "second")
    assert missing.reasons["rsi"] == "first"


# ---------------------------------------------------------------------------
# buckets
# ---------------------------------------------------------------------------


def test_relative_volume_buckets_are_aligned_with_the_rules_own_default():
    """1.5 is ``EntryRules.volume_multiple``'s default, so 'below 1.5 loses
    money' maps onto a change a user could actually make."""
    assert enrich.bucket_relative_volume(0.9) == "below_average"
    assert enrich.bucket_relative_volume(1.2) == "1.0-1.5"
    assert enrich.bucket_relative_volume(1.5) == "1.5-2.0"
    assert enrich.bucket_relative_volume(2.4) == "2.0-3.0"
    assert enrich.bucket_relative_volume(4.0) == "3.0_plus"
    assert enrich.bucket_relative_volume(None) is None


def test_atr_buckets_are_relative_to_the_samples_median():
    """Absolute ATR thresholds would not transfer between a small cap and a
    large cap, which is the whole reason the bucket is a ratio."""
    assert enrich.bucket_atr(1.0, 2.0) == "low"
    assert enrich.bucket_atr(2.0, 2.0) == "normal"
    assert enrich.bucket_atr(3.0, 2.0) == "elevated"
    assert enrich.bucket_atr(5.0, 2.0) == "extreme"
    assert enrich.bucket_atr(2.0, None) is None
    assert enrich.bucket_atr(None, 2.0) is None


def test_rsi_buckets_cover_the_range_without_gaps():
    for value in (10.0, 35.0, 50.0, 60.0, 85.0):
        assert enrich.bucket_rsi(value) is not None
    assert enrich.bucket_rsi(None) is None


def test_time_of_day_maps_a_daily_bar_to_the_open():
    """Every cached daily bar is stamped 09:15, so backtest trades are always
    at the open. Computed rather than assumed, because journal trades carry
    real execution times and both must share one vocabulary."""
    assert enrich.time_of_day(pd.Timestamp("2026-01-01 09:15")) == "open"
    assert enrich.time_of_day(pd.Timestamp("2026-01-01 10:30")) == "morning"
    assert enrich.time_of_day(pd.Timestamp("2026-01-01 12:00")) == "midday"
    assert enrich.time_of_day(pd.Timestamp("2026-01-01 14:00")) == "afternoon"
    assert enrich.time_of_day(pd.Timestamp("2026-01-01 15:30")) == "close"
    assert enrich.time_of_day(None) is None


def test_median_or_none_ignores_missing_values():
    assert enrich.median_or_none([1.0, None, 3.0]) == 2.0
    assert enrich.median_or_none([1.0, None, 3.0, 5.0]) == 3.0
    assert enrich.median_or_none([None, None]) is None
    assert enrich.median_or_none([]) is None


# ---------------------------------------------------------------------------
# regime labelling
# ---------------------------------------------------------------------------


def test_regime_is_labelled_from_the_entry_state_not_the_current_one():
    """A trade from six months ago must not be stamped with today's market.

    The breadth classifier in ``research/self_learning.py`` reads the whole
    universe as of *now*; reusing it here would make every regime finding a
    statement about today wearing the label of the past.
    """
    assert enrich.label_regime(5.0, 1.0, atr_median=2.0) == "uptrend"
    assert enrich.label_regime(-5.0, 1.0, atr_median=2.0) == "downtrend"
    assert enrich.label_regime(0.0, 1.0, atr_median=2.0) == "sideways"


def test_an_unknown_trend_yields_no_regime_rather_than_a_default():
    assert enrich.label_regime(None, 1.0, atr_median=2.0) is None


def test_a_volatility_shock_outranks_the_direction():
    """Direction and volatility are different questions; the volatile state is
    the one that changes how much the rest of the statistics can be trusted."""
    assert enrich.label_regime(5.0, 5.0, atr_median=2.0) == "high_volatility"


def test_benchmark_trend_uses_a_series_that_exists():
    """NIFTY-EQ is not in the cache; NIFTYBEES-EQ is, and is tradable."""
    frame = _flat_frame(80, close=np.linspace(100.0, 120.0, 80))
    benchmarks = {"NIFTYBEES-EQ": frame}
    value, source = enrich.benchmark_trend(benchmarks, frame["ts"].iloc[-1])
    assert value is not None and value > 5.0
    assert source == "NIFTYBEES-EQ"


def test_benchmark_trend_is_absent_when_no_proxy_is_cached():
    value, source = enrich.benchmark_trend({}, pd.Timestamp("2026-01-01"))
    assert value is None
    assert source is None


# ---------------------------------------------------------------------------
# sector table
# ---------------------------------------------------------------------------


def test_sector_table_reads_the_nifty_index_csvs(tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    (universe / "ind_nifty50list.csv").write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
        "Tata Consultancy Services Ltd.,Information Technology,TCS,EQ,INE467B01029\n",
        encoding="utf-8",
    )
    table = enrich.build_sector_table(tmp_path)
    assert table["RELIANCE"] == "Oil Gas & Consumable Fuels"
    assert table["TCS"] == "Information Technology"


def test_sector_table_canonicalises_the_series_suffix(tmp_path):
    """The cache and the CSV disagree about '-EQ'; the join must survive it."""
    universe = tmp_path / "universe"
    universe.mkdir()
    (universe / "ind_nifty50list.csv").write_text(
        "Company Name,Industry,Symbol,Series,ISIN Code\n"
        "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE-EQ,EQ,INE002A01018\n",
        encoding="utf-8",
    )
    table = enrich.build_sector_table(tmp_path)
    assert table["RELIANCE"] == "Oil Gas & Consumable Fuels"


def test_a_missing_universe_directory_yields_an_empty_table(tmp_path):
    assert enrich.build_sector_table(tmp_path) == {}


def test_a_malformed_csv_does_not_lose_the_others(tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    (universe / "ind_nifty50list.csv").write_text(
        "Company Name,Industry,Symbol\nReliance,Oil,RELIANCE\n", encoding="utf-8"
    )
    (universe / "ind_niftymidcap150list.csv").write_text(
        "Company Name,Industry,Symbol\nTata,IT,TCS\n", encoding="utf-8"
    )
    table = enrich.build_sector_table(tmp_path)
    assert table["RELIANCE"] == "Oil"
    assert table["TCS"] == "IT"
