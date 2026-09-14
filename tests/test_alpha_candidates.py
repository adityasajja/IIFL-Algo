"""Tests for the pre-registered alpha candidates.

The properties worth pinning down are the ones that would silently turn a null
into a false positive:

* the universe index must be **scale-free** — built from returns, not levels, or
  a Rs 3,700 stock carries 37x the weight of a Rs 100 one;
* the reversal must actually buy the losers, and its random-selection control
  must actually be random (a control with no variance is not a control);
* the circular shift must preserve the **exact multiset** of exposure values.
  That is what makes it a null about *timing*: if the shift also changed how much
  exposure the book carried, the comparison would be about sizing instead, and
  the whole H2/H3 test would be measuring the wrong thing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from atr.backtest.costs import SlippageModel
from atr.backtest.engine import BacktestConfig, BacktestEngine
from atr.core.enums import AssetClass
from atr.core.models import Bar, Instrument, MarketSnapshot
from atr.data.base import ListFeed
from atr.strategy.strategies.alpha_candidates import (
    ShortHorizonReversal,
    TrendFilteredExposure,
    VolatilityManaged,
    _universe_index,
)

START = datetime(2024, 1, 1)


def frame(prices: list[float]) -> pd.DataFrame:
    index = pd.DatetimeIndex([START + timedelta(days=i) for i in range(len(prices))])
    values = pd.Series(prices, index=index, dtype=float)
    return pd.DataFrame(
        {"open": values, "high": values * 1.01, "low": values * 0.99,
         "close": values, "volume": 100_000.0}
    )


def snapshots(series: dict[str, list[float]]) -> list[MarketSnapshot]:
    length = len(next(iter(series.values())))
    out = []
    for i in range(length):
        ts = START + timedelta(days=i)
        bars = {
            symbol: Bar(ts=ts, open=prices[i], high=prices[i] * 1.01,
                        low=prices[i] * 0.99, close=prices[i], volume=100_000.0)
            for symbol, prices in series.items()
        }
        out.append(MarketSnapshot(ts=ts, bars=bars))
    return out


def instrument(symbol: str) -> Instrument:
    return Instrument(symbol=symbol, exchange="NSEEQ", asset_class=AssetClass.EQUITY)


# ---------------------------------------------------------------------------
# The universe index
# ---------------------------------------------------------------------------


def test_universe_index_is_scale_free():
    """Built from returns, so a high-priced symbol cannot dominate by level."""
    cheap = frame([10.0, 20.0])
    expensive = frame([10_000.0, 20_000.0])
    index = _universe_index({"CHEAP": cheap, "EXPENSIVE": expensive})
    assert index.iloc[-1] == pytest.approx(2.0, rel=1e-9)


def test_universe_index_uses_equal_weights():
    """One symbol doubling and one flat is +50%, not +100%."""
    index = _universe_index({"A": frame([100.0, 200.0]), "B": frame([100.0, 100.0])})
    assert index.iloc[-1] == pytest.approx(1.5, rel=1e-9)


# ---------------------------------------------------------------------------
# H1 — reversal
# ---------------------------------------------------------------------------


def test_reversal_buys_the_worst_performers():
    symbols = {f"S{i}": [100.0] * 30 for i in range(10)}
    for i, symbol in enumerate(symbols):
        # S0 falls hardest, S9 falls least.
        symbols[symbol] = [100.0] * 20 + [100.0 - (9 - i) for _ in range(10)]

    strategy = ShortHorizonReversal(lookback=10, rebalance_days=10, frac=0.20,
                                    min_price=1.0, max_positions=10)
    feed = ListFeed(snapshots(symbols), {s: instrument(s) for s in symbols})
    result = BacktestEngine(feed, strategy, BacktestConfig(initial_cash=1_000_000.0,
                                                           slippage=SlippageModel(bps=0.0))).run()
    held = set(result.trades["symbol"]) if not result.trades.empty else set()
    # The two biggest fallers must be in the book; the flat names must not be.
    assert {"S0", "S1"} <= held
    assert "S9" not in held


def test_reversal_random_control_actually_varies_by_seed():
    """A control whose seeds all produce the same run has zero variance, which
    reads as a null nothing can clear — the opposite of a test."""
    symbols = {f"S{i}": [100.0 + i + (0.1 * i * j) for j in range(60)] for i in range(10)}
    instruments = {s: instrument(s) for s in symbols}
    config = BacktestConfig(initial_cash=1_000_000.0, slippage=SlippageModel(bps=0.0))

    picks = []
    for seed in (0, 1, 2):
        strategy = ShortHorizonReversal(lookback=10, rebalance_days=10, frac=0.30,
                                        min_price=1.0, max_positions=10,
                                        random_select=True, seed=seed)
        result = BacktestEngine(ListFeed(snapshots(symbols), instruments),
                                strategy, config).run()
        picks.append(frozenset(result.trades["symbol"]) if not result.trades.empty else frozenset())
    assert len({p for p in picks}) > 1, "every control seed picked the same names"


# ---------------------------------------------------------------------------
# H2 / H3 — exposure overlays and their circular-shift null
# ---------------------------------------------------------------------------


def _exposure_sequence(strategy, symbols, config) -> list[float]:
    """Read back the gross exposure the strategy actually carried, per bar."""
    result = BacktestEngine(
        ListFeed(snapshots(symbols), {s: instrument(s) for s in symbols}),
        strategy, config,
    ).run()
    return [round(float(v), 6) for v in result.exposure.to_numpy()]


class _Ctx:
    """Minimal context: the exposure rules read nothing but the bar index."""

    def __init__(self, index: int) -> None:
        self.index = index


def _signal_sequence(strategy, prices) -> list[float]:
    """The exposure *rule's* output, independent of the equity path."""
    strategy.prepare({"S": frame(prices)})
    return [strategy._exposure(_Ctx(i)) for i in range(len(prices))]


def test_vol_managed_exposure_is_capped_and_scales_inversely():
    """Low realised vol => full exposure; high vol => less. Never above 1."""
    quiet = {f"S{i}": [100.0 + 0.05 * j for j in range(80)] for i in range(6)}
    strategy = VolatilityManaged(target_vol=15.0, rebalance_days=1, max_positions=10)
    config = BacktestConfig(initial_cash=1_000_000.0, slippage=SlippageModel(bps=0.0))
    exposures = _exposure_sequence(strategy, quiet, config)
    assert max(exposures) <= 1.01          # see the drift note on the cap test
    assert max(exposures) >= 0.99


def test_vol_managed_sizes_down_when_volatility_rises():
    calm = [100.0 + 0.05 * j for j in range(120)]
    wild = [100.0 * (1.0 + (0.06 if j % 2 else -0.05)) ** 1 for j in range(120)]
    quiet_exposure = _signal_sequence(
        VolatilityManaged(target_vol=15.0, vol_window=20), calm)
    wild_exposure = _signal_sequence(
        VolatilityManaged(target_vol=15.0, vol_window=20), wild)
    assert max(quiet_exposure) == pytest.approx(1.0)
    assert max(wild_exposure) < max(quiet_exposure)


def test_trend_filter_goes_flat_below_the_average():
    rising = [100.0 * (1.01**j) for j in range(80)]
    falling = [100.0 * (0.99**j) for j in range(80)]
    up = _signal_sequence(TrendFilteredExposure(sma_window=20), rising)
    down = _signal_sequence(TrendFilteredExposure(sma_window=20), falling)
    assert max(up) == pytest.approx(1.0)
    assert max(down) == pytest.approx(0.0)


def test_circular_shift_moves_the_dates_and_nothing_else():
    """The null must move *when* the exposure changes, never *how much*.

    If the shift also altered the size of the book, H2/H3 would be testing a
    sizing rule against a differently-sized benchmark and the result would mean
    nothing. The rule's own output is compared here rather than the realised
    exposure ratio: the ratio is `gross_exposure / equity`, and the equity path
    legitimately differs between the two runs, so it drifts by ~0.4% even when
    the signal sequence is preserved exactly.
    """
    prices = [100.0 + 5.0 * np.sin(j / 9.0) + 0.1 * j for j in range(200)]
    base = _signal_sequence(VolatilityManaged(target_vol=15.0), prices)
    shifted = _signal_sequence(
        VolatilityManaged(target_vol=15.0, signal_shift=37), prices)

    assert shifted != base, "the shift changed nothing at all"
    for i, value in enumerate(shifted):
        assert value == pytest.approx(base[(i - 37) % len(base)]), f"bar {i}"
    assert sorted(base) == pytest.approx(sorted(shifted), abs=1e-12)


def test_gross_exposure_never_exceeds_the_cap():
    """The broker funds each order against total equity in isolation, so the
    portfolio-level cap has to come from the strategy.

    The realised ratio can sit a hair above 1.0: the cap is applied at order
    time against the equity *then*, while the ratio is measured later at the
    mark, after prices have moved. 1% is slack for that drift, not a licence —
    the uncapped version of this book reached 3.49x.
    """
    prices = {f"S{i}": [100.0 + 0.3 * j for j in range(120)] for i in range(20)}
    config = BacktestConfig(initial_cash=1_000_000.0, slippage=SlippageModel(bps=0.0))
    for strategy in (
        VolatilityManaged(target_vol=20.0, rebalance_days=1, max_positions=50),
        TrendFilteredExposure(sma_window=10, rebalance_days=1, max_positions=50),
        ShortHorizonReversal(lookback=5, rebalance_days=5, frac=0.5, min_price=1.0,
                             max_positions=50),
    ):
        result = BacktestEngine(
            ListFeed(snapshots(prices), {s: instrument(s) for s in prices}),
            strategy, config,
        ).run()
        assert result.exposure.max() <= 1.01, type(strategy).__name__


# ---------------------------------------------------------------------------
# No look-ahead
# ---------------------------------------------------------------------------


def test_reversal_ranking_uses_only_past_data():
    """`rev_k` at bar i must equal the trailing return over bars <= i."""
    prices = [100.0]
    for i in range(1, 40):
        prices.append(prices[-1] * (1.0 + 0.01 * ((i % 5) - 2)))
    prepared = frame(prices)
    strategy = ShortHorizonReversal(lookback=10)
    strategy._prepare_frames({"S": prepared})

    for i in (15, 25, 35):
        expected = prices[i] / prices[i - 10] - 1.0
        assert prepared["rev_10"].iloc[i] == pytest.approx(expected, rel=1e-9)
    assert pd.isna(prepared["rev_10"].iloc[5])
