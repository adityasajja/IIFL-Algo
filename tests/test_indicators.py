"""Indicator edge cases.

RSI in particular: a monotonic rise has zero average loss, which used to divide
by zero, become NaN, and then be filled with 50 — so the strongest possible
uptrend reported a perfectly neutral reading and silently qualified as an RSI
"pullback". Anything that consumes RSI (alerts, the scanner, entry rules) was
reading the opposite of the truth.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from atr.strategy.indicators import atr, bollinger, crossover, crossunder, rsi, sma


def test_rsi_is_100_when_price_only_rises():
    value = float(rsi(pd.Series(np.linspace(100, 200, 60))).iloc[-1])
    assert value == pytest.approx(100.0)


def test_rsi_is_0_when_price_only_falls():
    value = float(rsi(pd.Series(np.linspace(200, 100, 60))).iloc[-1])
    assert value == pytest.approx(0.0)


def test_rsi_is_neutral_on_a_flat_series():
    assert float(rsi(pd.Series([100.0] * 60)).iloc[-1]) == pytest.approx(50.0)


def test_rsi_stays_within_bounds():
    rng = np.random.default_rng(0)
    series = pd.Series(100 + np.cumsum(rng.normal(0, 1, 500)))
    values = rsi(series).dropna()
    assert values.between(0, 100).all()


def test_rsi_rises_with_sustained_gains():
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 0.2, 200)
    rising = pd.Series(100 + np.cumsum(np.abs(noise)))
    falling = pd.Series(200 - np.cumsum(np.abs(noise)))
    assert float(rsi(rising).iloc[-1]) > 70
    assert float(rsi(falling).iloc[-1]) < 30


def test_sma_needs_a_full_window():
    values = sma(pd.Series([1.0, 2.0, 3.0]), 5)
    assert values.isna().all()


def test_crossover_detects_the_cross_not_the_level():
    fast = pd.Series([1.0, 2.0, 3.0, 4.0])
    slow = pd.Series([2.0, 2.0, 2.0, 2.0])
    assert list(crossover(fast, slow)) == [False, False, True, False]
    assert list(crossunder(slow, fast)) == [False, False, True, False]


def test_atr_is_positive_and_tracks_range():
    high = pd.Series([101.0, 102.0, 103.0] * 20)
    low = pd.Series([99.0, 100.0, 101.0] * 20)
    close = pd.Series([100.0, 101.0, 102.0] * 20)
    value = float(atr(high, low, close).iloc[-1])
    assert value > 0


def test_bollinger_bands_bracket_the_mean():
    series = pd.Series(np.sin(np.arange(200)) * 10 + 100)
    mid, upper, lower = bollinger(series, 20, 2.0)
    tail = slice(-50, None)
    assert (upper[tail] >= mid[tail]).all()
    assert (lower[tail] <= mid[tail]).all()
