"""Event-driven backtest engine.

Loop per bar:
  1. advance the simulated broker (fills resting/queued orders at this bar)
  2. mark the portfolio to this bar's closes
  3. run pre-trade risk
  4. let the strategy submit orders (they fill from the *next* bar)

That ordering is what prevents look-ahead: a strategy acting on bar *i* can
never be filled at bar *i*'s close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd
from loguru import logger

from atr.backtest.broker import SimulatedBroker
from atr.backtest.costs import CommissionModel, SlippageModel
from atr.backtest.metrics import Metrics, compute_metrics
from atr.backtest.portfolio import Portfolio
from atr.backtest.trades import TradeLog
from atr.core.enums import OrderStatus
from atr.core.models import Instrument, MarketSnapshot, Order
from atr.data.base import DataFeed
from atr.execution.risk import RiskEngine, RiskLimits
from atr.strategy.base import Strategy, StrategyContext


@dataclass
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    commission: CommissionModel = field(default_factory=CommissionModel)
    slippage: SlippageModel = field(default_factory=SlippageModel)
    fill_on_next_open: bool = True
    participation_rate: float = 1.0
    equity_margin_ratio: float = 1.0
    allow_short: bool = True
    risk_free_rate: float = 0.0
    square_off_eod: bool = False
    risk: RiskLimits | None = None
    assert_invariants: bool = True
    #: Bars to run without letting the strategy act, so indicators can warm up
    #: before scoring begins. Frames and portfolio marks are still built, so a
    #: strategy that needs 200 bars of history can be evaluated over a test
    #: window shorter than that — otherwise it simply never trades, and "no
    #: trades" is indistinguishable from "no edge".
    warmup_bars: int = 0


@dataclass
class BacktestResult:
    metrics: Metrics
    equity: pd.Series
    cash: pd.Series
    exposure: pd.Series
    trades: pd.DataFrame
    fills: pd.DataFrame
    orders: pd.DataFrame
    strategy_name: str
    killed: bool = False
    kill_reason: str | None = None

    def summary(self) -> str:
        header = f"=== {self.strategy_name} ==="
        if self.killed:
            header += f"\n!! halted by risk: {self.kill_reason}"
        return f"{header}\n{self.metrics.summary()}"

    @property
    def total_return(self) -> float:
        return self.metrics.total_return_pct


class BacktestEngine:
    def __init__(
        self,
        feed: DataFeed,
        strategy: Strategy,
        config: BacktestConfig | None = None,
    ) -> None:
        self.feed = feed
        self.strategy = strategy
        self.config = config or BacktestConfig()
        self.portfolio = Portfolio(
            initial_cash=self.config.initial_cash,
            equity_margin_ratio=self.config.equity_margin_ratio,
            allow_short=self.config.allow_short,
        )
        self.broker = SimulatedBroker(
            portfolio=self.portfolio,
            commission=self.config.commission,
            slippage=self.config.slippage,
            fill_on_next_open=self.config.fill_on_next_open,
            participation_rate=self.config.participation_rate,
        )
        self.trade_log = TradeLog()
        self.risk = RiskEngine(self.config.risk or RiskLimits())
        self.instruments: dict[str, Instrument] = {}
        self.frames: dict[str, pd.DataFrame] = {}
        self._exposures: list[tuple[datetime, float]] = []
        self._killed = False
        self._kill_reason: str | None = None
        self._current_ts: datetime | None = None

    # ------------------------------------------------------------------
    def _build_frames(self, snapshots: list[MarketSnapshot]) -> None:
        """One aligned DataFrame per symbol over the shared time grid."""
        index = pd.DatetimeIndex([s.ts for s in snapshots])
        columns = ["open", "high", "low", "close", "volume"]
        for symbol in self.instruments:
            data = []
            for snap in snapshots:
                bar = snap.bars.get(symbol)
                data.append(
                    [bar.open, bar.high, bar.low, bar.close, bar.volume] if bar
                    else [float("nan")] * 5
                )
            self.frames[symbol] = pd.DataFrame(data, index=index, columns=columns)

    # ------------------------------------------------------------------
    def run(self) -> BacktestResult:
        snapshots = self.feed.load()
        if not snapshots:
            raise ValueError("feed produced no data")
        self.instruments = self.feed.instruments
        self._build_frames(snapshots)

        self.strategy.prepare(self.frames)

        ctx = StrategyContext(
            instruments=self.instruments,
            frames=self.frames,
            submit=self._submit,
            portfolio_getter=lambda: self.portfolio,
        )
        self.risk.reset()
        self.strategy.on_start(ctx)

        current_day: datetime.date | None = None
        for i, snap in enumerate(snapshots):
            self._current_ts = snap.ts
            # --- session rollover -------------------------------------
            if current_day is not None and snap.ts.date() != current_day:
                self.strategy.on_session_end(ctx)
                if self.config.square_off_eod:
                    ctx.close_all()
                self.risk.reset_daily()
            current_day = snap.ts.date()

            # --- fills --------------------------------------------------
            for fill in self.broker.on_bar(snap):
                self.trade_log.add(fill)

            # --- mark ---------------------------------------------------
            prices = {s: b.close for s, b in snap.bars.items()}
            self.portfolio.mark(snap.ts, prices)
            self._exposures.append(
                (snap.ts, self.portfolio.gross_exposure / max(self.portfolio.equity, 1e-9))
            )

            # --- risk ---------------------------------------------------
            if not self._killed:
                verdict = self.risk.check(self.portfolio, snap.ts)
                if not verdict.allowed:
                    logger.warning("risk halt: {}", verdict.reason)
                    self._killed = True
                    self._kill_reason = verdict.reason
                    ctx.close_all()

            # --- strategy ------------------------------------------------
            ctx.now = snap.ts
            ctx.index = i
            ctx.bars = snap.bars
            for symbol, bar in snap.bars.items():
                ctx.windows[symbol].push(bar)
            if self._killed or i < self.config.warmup_bars:
                continue
            self.strategy.on_bar(ctx)

            if self.config.assert_invariants:
                self.portfolio.check_invariant()

        self.strategy.on_stop(ctx)
        return self._result()

    # ------------------------------------------------------------------
    def _submit(self, order: Order) -> Order:
        verdict = self.risk.check_order(order, self.portfolio)
        if not verdict.allowed:
            order.mark(OrderStatus.REJECTED, reason=verdict.reason)
            return order
        return self.broker.submit(order, now=self._current_ts)

    # ------------------------------------------------------------------
    def _result(self) -> BacktestResult:
        equity = self.portfolio.equity_curve()
        exposure = pd.Series(
            [e for _, e in self._exposures],
            index=pd.DatetimeIndex([t for t, _ in self._exposures]),
            name="exposure",
        )
        metrics = compute_metrics(
            equity,
            self.trade_log.frame(),
            risk_free_rate=self.config.risk_free_rate,
            total_commission=self.portfolio.commission_paid,
            total_slippage=self.portfolio.slippage_cost,
            exposure=exposure,
            final_positions=sum(1 for p in self.portfolio.positions.values() if not p.is_flat),
        )
        return BacktestResult(
            metrics=metrics,
            equity=equity,
            cash=self.portfolio.cash_curve(),
            exposure=exposure,
            trades=self.trade_log.frame(),
            fills=_fills_frame(self.broker.fills),
            orders=_orders_frame(self.broker.submitted),
            strategy_name=self.strategy.name or type(self.strategy).__name__,
            killed=self._killed,
            kill_reason=self._kill_reason,
        )


def _fills_frame(fills: list) -> pd.DataFrame:
    if not fills:
        return pd.DataFrame(
            columns=["ts", "symbol", "side", "quantity", "price", "commission", "slippage"]
        )
    return pd.DataFrame(
        [
            {
                "ts": f.ts,
                "symbol": f.instrument.symbol,
                "side": f.side.name,
                "quantity": f.quantity,
                "price": f.price,
                "commission": f.commission,
                "slippage": f.slippage,
            }
            for f in fills
        ]
    )


def _orders_frame(orders: list[Order]) -> pd.DataFrame:
    if not orders:
        return pd.DataFrame(
            columns=["order_id", "symbol", "side", "quantity", "type", "status", "filled"]
        )
    return pd.DataFrame(
        [
            {
                "order_id": o.order_id,
                "symbol": o.instrument.symbol,
                "side": o.side.name,
                "quantity": o.quantity,
                "type": o.order_type.value,
                "status": o.status.value,
                "filled": o.filled_quantity,
                "avg_fill_price": o.avg_fill_price,
                "reject_reason": o.reject_reason,
            }
            for o in orders
        ]
    )
