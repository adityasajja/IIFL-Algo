"""Tests for the MomentumBreakout strategy.

Uses deterministic price sequences via ListFeed so the entry, exit, and
cooldown logic can be verified bar by bar.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from atr.backtest.costs import SlippageModel
from atr.backtest.engine import BacktestConfig, BacktestEngine
from atr.core.enums import AssetClass
from atr.core.models import Bar, Instrument, MarketSnapshot
from atr.data.base import ListFeed
from atr.strategy.strategies import MomentumBreakout

EQ = Instrument(symbol="TEST", asset_class=AssetClass.EQUITY, currency="INR")

_START = datetime(2024, 1, 1, 9, 30)


def _make_bar(ts: datetime, close: float, high=None, low=None, volume=10_000) -> Bar:
    if high is None:
        high = close * 1.01
    if low is None:
        low = close * 0.99
    return Bar(ts=ts, open=close, high=high, low=low, close=close, volume=volume)


def _feed_with_volumes(prices, volumes, symbol="TEST"):
    snaps = [
        MarketSnapshot(
            ts=_START + timedelta(minutes=i),
            bars={symbol: _make_bar(_START + timedelta(minutes=i), p, volume=v)},
        )
        for i, (p, v) in enumerate(zip(prices, volumes))
    ]
    return ListFeed(snaps, {symbol: EQ})


def _feed(prices, symbol="TEST", base_volume=10_000):
    """Feed where the last ``len(prices) - len(base_prices)`` bars have elevated volume.

    Actually, just use a simple approach: all bars get base_volume, and we
    override volumes for breakout bars in the specific tests.
    """
    volumes = [base_volume] * len(prices)
    return _feed_with_volumes(prices, volumes)


def test_strategy_registered():
    from atr.strategy.strategies import STRATEGIES
    assert "momentum_breakout" in STRATEGIES
    assert STRATEGIES["momentum_breakout"] is MomentumBreakout


def test_no_position_below_min_bars():
    """Strategy should not trade during warmup."""
    prices = [100.0] * 5  # 5 bars, min_bars=30
    feed = _feed(prices)
    strategy = MomentumBreakout()
    result = BacktestEngine(feed, strategy, BacktestConfig(initial_cash=1_000_000, slippage=SlippageModel(bps=0))).run()
    assert result.trades.empty
    assert result.metrics.num_trades == 0


def test_breakout_enters_long():
    """Price breaking above the lookback high with volume should enter long."""
    prices = [100.0] * 30  # 30 bars at 100
    # Break out above 100 with 5x volume
    prices += [102.0, 103.0, 104.0, 105.0]
    volumes = [10_000] * 30 + [50_000] * 4
    feed = _feed_with_volumes(prices, volumes)
    strategy = MomentumBreakout(lookahead=20, min_bars=25, volume_multiple=1.0, trend_filter=None, consecutive_breakout=1)
    result = BacktestEngine(feed, strategy, BacktestConfig(initial_cash=1_000_000, slippage=SlippageModel(bps=0))).run()
    assert (result.fills["side"] == "BUY").sum() >= 1


def test_stop_loss_exits():
    """A drop to the stop-loss level should close the position."""
    prices = [100.0] * 30  # warm up at 100
    prices += [102.0, 103.0]  # breakout, entry ~103
    # Stop loss = 103 * 0.97 = 99.91; drop well below that
    stop = 103.0 * (1 - 3.0 / 100.0)
    prices += [stop - 2.0, stop - 2.0, stop - 2.0]
    volumes = [10_000] * 30 + [50_000] * 2 + [10_000] * 3
    feed = _feed_with_volumes(prices, volumes)
    strategy = MomentumBreakout(lookahead=20, min_bars=25, volume_multiple=1.0, trend_filter=None, consecutive_breakout=2, cooldown_bars=1)
    result = BacktestEngine(feed, strategy, BacktestConfig(initial_cash=1_000_000, slippage=SlippageModel(bps=0))).run()
    assert len(result.fills) >= 2
    assert (result.fills["side"] == "BUY").sum() >= 1
    assert (result.fills["side"] == "SELL").sum() >= 1
    assert result.metrics.final_positions == 0


def test_take_profit_exits():
    """A move to the take-profit level should close the position."""
    prices = [100.0] * 30  # warm up at 100
    prices += [102.0, 103.0]  # breakout, entry ~103
    # Take profit = 103 * 1.06 = 109.18; push price above that
    prices += [110.0, 111.0, 112.0]
    volumes = [10_000] * 30 + [50_000] * 2 + [10_000] * 3
    feed = _feed_with_volumes(prices, volumes)
    strategy = MomentumBreakout(lookahead=20, min_bars=25, volume_multiple=1.0, trend_filter=None, consecutive_breakout=2, cooldown_bars=1)
    result = BacktestEngine(feed, strategy, BacktestConfig(initial_cash=1_000_000, slippage=SlippageModel(bps=0))).run()
    assert len(result.fills) >= 2
    assert (result.fills["side"] == "SELL").sum() >= 1
    assert result.metrics.final_positions == 0


def test_cooldown_prevents_reentry():
    """After an exit, the symbol should be skipped for cooldown_bars."""
    prices = [100.0] * 30  # warm up at 100
    prices += [102.0, 103.0]  # breakout, entry ~103
    stop = 103.0 * (1 - 3.0 / 100.0)
    prices += [stop - 2.0, stop - 2.0, stop - 2.0]  # exit via stop loss
    # Price recovers above lookback high — should NOT re-enter during cooldown
    prices += [105.0, 106.0, 107.0]
    volumes = [10_000] * 30 + [50_000] * 2 + [10_000] * 3 + [50_000] * 3
    feed = _feed_with_volumes(prices, volumes)
    strategy = MomentumBreakout(
        lookahead=20, min_bars=25, volume_multiple=1.0, trend_filter=None,
        consecutive_breakout=2, cooldown_bars=5,
    )
    result = BacktestEngine(feed, strategy, BacktestConfig(initial_cash=1_000_000, slippage=SlippageModel(bps=0))).run()
    buy_count = (result.fills["side"] == "BUY").sum()
    assert buy_count == 1  # only the initial entry


def test_equity_curve_has_no_nans():
    """Strategy should not produce NaN equity values."""
    prices = [100.0 + i * 0.5 for i in range(100)]  # steadily rising
    volumes = [10_000] * 100
    feed = _feed_with_volumes(prices, volumes)
    strategy = MomentumBreakout(lookahead=20, min_bars=10, trend_filter=None)
    result = BacktestEngine(feed, strategy, BacktestConfig(initial_cash=1_000_000, slippage=SlippageModel(bps=0))).run()
    assert not result.equity.isna().any()
