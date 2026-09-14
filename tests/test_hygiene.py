"""Data hygiene tests.

The detector has to do two opposite things at once, and getting either wrong is
silent:

* **Drop the vendor glitch.** A 3x bar on a quiet tape is indistinguishable from
  an Episodic Pivot — a huge gap, abnormal volume, in a stock that had done
  nothing for months. Left in, the backtest buys it and the premise study reports
  a +264% twenty-day move that never happened. Fourteen such bars sit on
  2021-09-15/16 in the real cache.
* **Keep the genuine repricing.** A filter that drops anything far from its
  neighbours would delete exactly the episodic moves the research is trying to
  measure. The distinguishing feature is *reversion*: a real move does not
  come back.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from atr.data.hygiene import drop_reverting_spikes, reverting_spike_mask

START = datetime(2024, 1, 1)


def series(prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": [START + timedelta(days=i) for i in range(len(prices))],
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [1000.0] * len(prices),
        }
    )


def test_drops_a_single_bar_spike_that_reverts():
    prices = [100.0] * 10 + [300.0] + [100.0] * 10
    clean, dropped = drop_reverting_spikes(series(prices))
    assert len(dropped) == 1
    assert len(clean) == len(prices) - 1
    assert 300.0 not in clean["close"].tolist()


def test_drops_a_two_bar_corrupt_segment():
    """The real BEL/IRCTC glitch spans two sessions — one bar is not enough."""
    prices = [100.0] * 10 + [300.0, 295.0] + [100.0] * 10
    clean, dropped = drop_reverting_spikes(series(prices))
    assert len(dropped) == 2


def test_keeps_a_genuine_doubling():
    """A real re-rating must survive: it does not come back."""
    prices = [100.0] * 10 + [200.0] * 12
    clean, dropped = drop_reverting_spikes(series(prices))
    assert len(dropped) == 0
    assert len(clean) == len(prices)


def test_keeps_a_gradual_trend():
    prices = [100.0 * (1.02**i) for i in range(30)]
    _, dropped = drop_reverting_spikes(series(prices))
    assert len(dropped) == 0


def test_keeps_a_sustained_threefold_move():
    """The guard is reversion, not magnitude."""
    prices = [50.0] * 10 + [150.0] * 12
    _, dropped = drop_reverting_spikes(series(prices))
    assert len(dropped) == 0


def test_drops_a_crash_and_recovery():
    prices = [100.0] * 10 + [20.0] + [100.0] * 10
    _, dropped = drop_reverting_spikes(series(prices))
    assert len(dropped) == 1


def test_real_cache_bel_glitch_is_caught():
    """The bar that produced a fake +264% 20-session move in the premise study."""
    prices = (
        [57.42, 57.95, 58.17, 58.12, 58.68, 58.03, 57.02, 58.38, 59.22, 59.98]
        + [60.43, 61.77, 61.35, 62.22, 63.28, 63.38, 66.20, 65.15, 64.77, 65.70]
        + [65.68, 65.50, 67.47, 207.80, 205.55, 68.43]
        + [68.62, 69.12, 69.05, 68.44, 68.90, 69.40, 70.10, 70.55]
    )
    mask = reverting_spike_mask(pd.Series(prices))
    flagged = [i for i, v in enumerate(mask) if v]
    assert 23 in flagged and 24 in flagged
    assert 22 not in flagged and 25 not in flagged


def test_empty_and_short_frames_are_safe():
    empty = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    clean, dropped = drop_reverting_spikes(empty)
    assert clean.empty and dropped.empty

    short = series([100.0, 100.0])
    _, dropped = drop_reverting_spikes(short)
    assert dropped.empty


def test_missing_close_column_is_a_no_op():
    frame = pd.DataFrame({"ts": [START], "open": [1.0]})
    clean, dropped = drop_reverting_spikes(frame)
    assert len(clean) == 1 and dropped.empty


def test_dropped_rows_carry_the_original_data():
    """The caller must be able to say what was removed — a silent repair is
    indistinguishable from a silent bug."""
    prices = [100.0] * 10 + [300.0] + [100.0] * 10
    _, dropped = drop_reverting_spikes(series(prices))
    assert dropped.iloc[0]["close"] == pytest.approx(300.0)
    assert dropped.iloc[0]["ts"] == START + timedelta(days=10)
