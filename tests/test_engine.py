"""Core engine tests.

These are the checks that matter: no look-ahead in fills, portfolio
conservation, cost attribution, and risk halting.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from atr.backtest.costs import CommissionModel, SlippageModel
from atr.backtest.engine import BacktestConfig, BacktestEngine
from atr.backtest.metrics import compute_metrics, max_drawdown
from atr.backtest.portfolio import Portfolio
from atr.core.enums import AssetClass, OrderStatus, OrderType, Side
from atr.core.models import Bar, Fill, Instrument, MarketSnapshot, Order
from atr.data.synthetic import SyntheticConfig, SyntheticFeed
from atr.execution.risk import RiskEngine, RiskLimits
from atr.strategy.base import Strategy
from atr.strategy.strategies.sma_crossover import SmaCrossover

EQ = Instrument(symbol="TEST", asset_class=AssetClass.EQUITY, currency="INR")
FUT = Instrument(
    symbol="TESTFUT",
    asset_class=AssetClass.FUTURE,
    multiplier=100,
    initial_margin_per_unit=25,
    quantity_step=50,
)


def _snapshots(prices, symbol="TEST", start=datetime(2024, 1, 1, 9, 30)):
    out = []
    for price in prices:
        bar = Bar(
            ts=start,
            open=price,
            high=price * 1.01,
            low=price * 0.99,
            close=price,
            volume=10_000,
        )
        out.append(MarketSnapshot(ts=bar.ts, bars={symbol: bar}))
    return out


# --------------------------------------------------------------------------
def test_position_pnl_long_roundtrip():
    portfolio = Portfolio(initial_cash=100_000)
    buy = Fill(
        order_id="1", instrument=EQ, side=Side.BUY, quantity=10, price=100,
        ts=datetime(2024, 1, 1), commission=1.0,
    )
    portfolio.apply_fill(buy)
    assert portfolio.cash == pytest.approx(100_000 - 1000 - 1.0)
    assert portfolio.commission_paid == pytest.approx(1.0)
    assert portfolio.position("TEST").quantity == 10

    sell = Fill(order_id="2", instrument=EQ, side=Side.SELL, quantity=10, price=110, ts=datetime(2024, 1, 2))
    realised = portfolio.apply_fill(sell)
    assert realised == pytest.approx(100.0)
    assert portfolio.position("TEST").is_flat
    portfolio.check_invariant()


def test_position_flip_through_zero():
    portfolio = Portfolio(initial_cash=1_000_000)
    portfolio.apply_fill(Fill(order_id="1", instrument=EQ, side=Side.BUY, quantity=10, price=100, ts=datetime(2024, 1, 1)))
    portfolio.apply_fill(Fill(order_id="2", instrument=EQ, side=Side.SELL, quantity=25, price=110, ts=datetime(2024, 1, 2)))
    pos = portfolio.position("TEST")
    assert pos.quantity == -15
    assert pos.avg_price == pytest.approx(110)
    portfolio.check_invariant()


def test_market_order_fills_next_bar_open_not_signal_close():
    """The anti-look-ahead guarantee."""
    from atr.backtest.broker import SimulatedBroker

    portfolio = Portfolio(initial_cash=1_000_000)
    broker = SimulatedBroker(portfolio, fill_on_next_open=True)
    order = Order(instrument=EQ, side=Side.BUY, quantity=10, order_type=OrderType.MARKET)

    broker.on_bar(MarketSnapshot(ts=datetime(2024, 1, 1), bars={"TEST": Bar(ts=datetime(2024, 1, 1), open=100, high=101, low=99, close=150, volume=1000)}))
    broker.submit(order, now=datetime(2024, 1, 1))
    assert broker.fills == []  # nothing filled on the signal bar

    broker.on_bar(MarketSnapshot(ts=datetime(2024, 1, 2), bars={"TEST": Bar(ts=datetime(2024, 1, 2), open=120, high=121, low=119, close=125, volume=1000)}))
    assert len(broker.fills) == 1
    assert broker.fills[0].price >= 120  # open + slippage, never the 150 close


def test_limit_order_fills_at_limit_not_through():
    from atr.backtest.broker import SimulatedBroker

    portfolio = Portfolio(initial_cash=1_000_000)
    broker = SimulatedBroker(portfolio, slippage=SlippageModel(bps=0))
    order = Order(instrument=EQ, side=Side.BUY, quantity=10, order_type=OrderType.LIMIT, limit_price=95)
    broker.submit(order)
    # Gaps down through the limit: should fill at the open (95 < 90? no) -> open is 80, below limit
    broker.on_bar(MarketSnapshot(ts=datetime(2024, 1, 1), bars={"TEST": Bar(ts=datetime(2024, 1, 1), open=80, high=85, low=70, close=82, volume=1000)}))
    assert len(broker.fills) == 1
    assert broker.fills[0].price == pytest.approx(80.0)


def test_insufficient_margin_rejects_order():
    from atr.backtest.broker import SimulatedBroker

    portfolio = Portfolio(initial_cash=1_000, equity_margin_ratio=1.0)
    broker = SimulatedBroker(portfolio)
    order = Order(instrument=EQ, side=Side.BUY, quantity=1_000, order_type=OrderType.MARKET)
    broker.submit(order)
    broker.on_bar(MarketSnapshot(ts=datetime(2024, 1, 1), bars={"TEST": Bar(ts=datetime(2024, 1, 1), open=100, high=101, low=99, close=100, volume=1000)}))
    assert order.status is OrderStatus.REJECTED


def test_futures_margin_is_per_lot_not_notional():
    portfolio = Portfolio(initial_cash=100_000)
    portfolio.apply_fill(Fill(order_id="1", instrument=FUT, side=Side.BUY, quantity=50, price=100, ts=datetime(2024, 1, 1)))
    assert portfolio.margin_used == pytest.approx(50 * 25)
    notional = 50 * 100 * 100
    assert portfolio.margin_used < notional


def test_max_drawdown():
    equity = pd.Series([100, 120, 90, 95, 130], index=pd.date_range("2024-01-01", periods=5))
    dd, length = max_drawdown(equity)
    assert dd == pytest.approx(0.25)
    assert length == 2


def test_metrics_on_known_series():
    idx = pd.date_range("2024-01-01", periods=253, freq="D")
    equity = pd.Series(np.linspace(100, 120, 253), index=idx)
    metrics = compute_metrics(equity, pd.DataFrame(), risk_free_rate=0.0)
    assert metrics.total_return_pct == pytest.approx(20.0, abs=0.1)
    assert metrics.max_drawdown_pct == pytest.approx(0.0, abs=0.01)
    assert metrics.sharpe > 0


def test_risk_daily_loss_halts():
    portfolio = Portfolio(initial_cash=100_000)
    risk = RiskEngine(RiskLimits(max_daily_loss=1_000))
    risk.check(portfolio, datetime(2024, 1, 1))
    portfolio.cash -= 5_000
    verdict = risk.check(portfolio, datetime(2024, 1, 2))
    assert not verdict.allowed
    assert risk.halted


def test_risk_blocks_short_when_disabled():
    portfolio = Portfolio(initial_cash=100_000)
    risk = RiskEngine(RiskLimits(allow_short=False))
    order = Order(instrument=EQ, side=Side.SELL, quantity=10)
    assert not risk.check_order(order, portfolio).allowed


class _BuyOnce(Strategy):
    """Buys on bar 0 and never sells — used to exercise the full loop."""

    name = "buy_once"

    def __init__(self):
        super().__init__()
        self.done = False

    def on_bar(self, ctx) -> None:
        if not self.done:
            ctx.order("AAPL", 100, tag="entry")
            self.done = True


def test_engine_end_to_end_on_synthetic_data():
    feed = SyntheticFeed(
        SyntheticConfig(symbols=("AAPL",), start=datetime(2024, 1, 1, 9, 30), end=datetime(2024, 3, 1, 15, 59))
    )
    result = BacktestEngine(feed, _BuyOnce(), BacktestConfig(initial_cash=500_000)).run()
    assert len(result.equity) > 100
    assert result.metrics.end_equity > 0
    assert (result.fills["side"] == "BUY").any()


def test_sma_crossover_runs_and_respects_costs():
    feed = SyntheticFeed(
        SyntheticConfig(symbols=("AAPL", "MSFT"), start=datetime(2024, 1, 1, 9, 30), end=datetime(2024, 6, 28, 15, 59))
    )
    config = BacktestConfig(
        initial_cash=1_000_000,
        commission=CommissionModel(per_share=0.005, min_per_order=1.0),
        slippage=SlippageModel(bps=5),
    )
    result = BacktestEngine(feed, SmaCrossover(fast=10, slow=30), config).run()
    assert result.metrics.total_commission > 0
    assert not result.equity.isna().any()
    assert result.metrics.num_trades >= 1
