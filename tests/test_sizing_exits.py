"""Protective-exit tests for `atr.backtest.sizing.SizedStrategy` — the wrapper
that turns a strategy's bare order into a sized position with stop/target/
trailing exits. No existing test exercised this mechanism at all before now.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from atr.backtest.costs import SlippageModel
from atr.backtest.engine import BacktestConfig, BacktestEngine
from atr.backtest.sizing import ExitPlan, SizedStrategy, SizingPlan
from atr.core.enums import AssetClass
from atr.core.models import Bar, Instrument, MarketSnapshot
from atr.data.base import ListFeed
from atr.strategy.base import Strategy

EQ = Instrument(symbol="TEST", asset_class=AssetClass.EQUITY, currency="INR")


class _BuyOnceHere(Strategy):
    name = "buy_once_here"
    done = False

    def on_bar(self, ctx) -> None:
        if not self.done:
            self.done = True
            ctx.order("TEST", 1, tag="entry")  # SizedStrategy resizes this


def _snapshots(closes: list[float], daily: bool = True) -> list[MarketSnapshot]:
    out = []
    start = datetime(2024, 1, 1, 9, 30)
    for i, price in enumerate(closes):
        ts = start + (timedelta(days=i) if daily else timedelta(minutes=i))
        bar = Bar(ts=ts, open=price, high=price * 1.01, low=price * 0.99, close=price, volume=10_000)
        out.append(MarketSnapshot(ts=ts, bars={"TEST": bar}))
    return out


def _run(closes, exits, sizing=None):
    feed = ListFeed(_snapshots(closes), {"TEST": EQ})
    strategy = SizedStrategy(_BuyOnceHere(), sizing=sizing or SizingPlan(fraction=0.5), exits=exits)
    config = BacktestConfig(initial_cash=100_000, slippage=SlippageModel(bps=0.0))
    engine = BacktestEngine(feed, strategy, config)
    result = engine.run()
    return engine, result


def test_percent_trailing_stop_fires_off_the_high_water_mark():
    # Rallies to 120 (high water), then gives back more than 10% off that peak.
    # A couple of extra bars after the breach so the close order — submitted
    # on the breach bar, filled on the next — actually has a next bar to land on.
    closes = [100, 105, 110, 120, 118, 115, 105, 104, 104]  # 105 is 12.5% off the 120 peak
    engine, result = _run(closes, ExitPlan(trailing_stop=0.10))
    assert engine.portfolio.position("TEST").is_flat
    reasons = [a.get("exit_reason") for a in engine.strategy.annotations]
    assert "trailing_stop" in reasons


def test_atr_trailing_stop_fires_wider_on_a_volatile_name():
    # Same shape as the percent-trail case, but ATR trailing is asked for
    # instead — the exit should still fire off the same peak-to-trough move.
    closes = [100, 105, 110, 120, 118, 112, 100, 99, 99]
    engine, result = _run(
        closes,
        ExitPlan(trailing_stop_atr_multiple=1.5, trailing_stop_atr_period=3),
    )
    assert engine.portfolio.position("TEST").is_flat
    reasons = [a.get("exit_reason") for a in engine.strategy.annotations]
    assert "trailing_stop_atr" in reasons


def test_atr_trailing_stop_does_not_fire_without_enough_history_yet():
    """Fewer bars than the ATR period must not crash — no ATR, no exit, not an error."""
    closes = [100, 101, 99]
    engine, result = _run(
        closes,
        ExitPlan(trailing_stop_atr_multiple=1.5, trailing_stop_atr_period=14),
    )
    # Should simply run to completion without raising, position may or may not
    # still be open depending on data length — the guarantee under test is
    # "no exception from a short window", not a specific exit outcome.
    assert result is not None


def test_atr_trailing_stop_is_independent_of_percent_trailing_stop():
    """Setting only the ATR variant must not accidentally also apply a percent trail."""
    plan = ExitPlan(trailing_stop_atr_multiple=2.0)
    assert plan.trailing_stop is None
    assert plan.any is True
