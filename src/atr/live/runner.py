"""Live trading runner.

Wires the same Strategy/Portfolio/Risk objects used in backtests to the IIFL
bridge feed and the IIFL broker:

    warmup history -> strategy.prepare() -> live bars -> strategy.on_bar()
                                                     -> risk -> broker

Warmup matters: indicators computed on a short window would otherwise produce
nonsense signals for the first N bars of the session.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime

import pandas as pd
from loguru import logger

from atr.backtest.portfolio import Portfolio
from atr.brokers.base import Broker
from atr.brokers.iifl.auth import Session
from atr.brokers.iifl.feeds import IiflLiveFeed
from atr.core.models import Instrument
from atr.execution.risk import RiskEngine, RiskLimits
from atr.strategy.base import Strategy, StrategyContext


@dataclass
class LiveConfig:
    freq: str = "1min"
    poll_timeout: float = 1.0
    square_off_time: dtime = dtime(15, 15)
    warmup_bars: int = 500
    max_runtime_seconds: float | None = None
    flatten_on_stop: bool = True


class LiveRunner:
    def __init__(
        self,
        session: Session,
        instruments: dict[str, Instrument],
        strategy: Strategy,
        broker: Broker,
        warmup_frames: dict[str, pd.DataFrame] | None = None,
        config: LiveConfig | None = None,
        risk: RiskLimits | None = None,
        initial_cash: float = 1_000_000.0,
    ) -> None:
        self.session = session
        self.instruments = instruments
        self.strategy = strategy
        self.broker = broker
        self.config = config or LiveConfig()
        self.portfolio = Portfolio(initial_cash=initial_cash)
        self.risk = RiskEngine(risk or RiskLimits())
        self.frames: dict[str, pd.DataFrame] = warmup_frames or {}
        self.feed = IiflLiveFeed(session, instruments, freq=self.config.freq)
        self.trades: list = []
        self._stop = False
        self._squared_off_date: datetime.date | None = None

    # ------------------------------------------------------------------
    def _ensure_frames(self) -> None:
        for symbol in self.instruments:
            if symbol not in self.frames:
                self.frames[symbol] = pd.DataFrame(
                    columns=["open", "high", "low", "close", "volume"]
                )
        self.strategy.prepare(self.frames)

    def _append_bar(self, symbol: str, bar) -> None:
        frame = self.frames[symbol]
        row = pd.DataFrame(
            [[bar.open, bar.high, bar.low, bar.close, bar.volume]],
            index=pd.DatetimeIndex([bar.ts]),
            columns=["open", "high", "low", "close", "volume"],
        )
        self.frames[symbol] = pd.concat([frame, row])

    # ------------------------------------------------------------------
    def _submit(self, order):
        verdict = self.risk.check_order(order, self.portfolio)
        if not verdict.allowed:
            logger.warning("order blocked by risk: {}", verdict.reason)
            order.reject_reason = verdict.reason
            return order
        logger.info(
            "submitting {} {} {} @ {}",
            order.side.name,
            order.quantity,
            order.instrument.symbol,
            order.order_type.value,
        )
        return self.broker.place_order(order)

    # ------------------------------------------------------------------
    def run(self) -> None:
        self._ensure_frames()
        ctx = StrategyContext(
            instruments=self.instruments,
            frames=self.frames,
            submit=self._submit,
            portfolio_getter=lambda: self.portfolio,
        )
        self.feed.start()
        self.strategy.on_start(ctx)
        started = time.time()
        logger.info("live runner started on {}", list(self.instruments))

        try:
            while not self._stop:
                snapshot = self.feed.get(timeout=self.config.poll_timeout)
                if snapshot is None:
                    if (
                        self.config.max_runtime_seconds
                        and time.time() - started > self.config.max_runtime_seconds
                    ):
                        break
                    continue

                for symbol, bar in snapshot.bars.items():
                    self._append_bar(symbol, bar)
                    position = self.portfolio.position(symbol)
                    if not position.is_flat:
                        position.mark(bar.close, bar.ts)

                now = snapshot.ts
                prices = {s: b.close for s, b in snapshot.bars.items()}
                self.portfolio.mark(now, prices)

                verdict = self.risk.check(self.portfolio, now)
                if not verdict.allowed:
                    logger.error("risk halt: {} — flattening", verdict.reason)
                    self.flatten(ctx)
                    break

                # Mandatory intraday square-off.
                if now.time() >= self.config.square_off_time and self._squared_off_date != now.date():
                    self._squared_off_date = now.date()
                    logger.info("square-off time reached")
                    self.flatten(ctx)

                ctx.now = now
                ctx.bars = snapshot.bars
                for symbol in self.instruments:
                    ctx.index = max(len(self.frames[symbol]) - 1, 0)
                for symbol, bar in snapshot.bars.items():
                    ctx.windows[symbol].push(bar)
                self.strategy.on_bar(ctx)

                if (
                    self.config.max_runtime_seconds
                    and time.time() - started > self.config.max_runtime_seconds
                ):
                    break
        except KeyboardInterrupt:
            logger.warning("interrupted")
        finally:
            if self.config.flatten_on_stop:
                self.flatten(ctx)
            self.strategy.on_stop(ctx)
            self.feed.stop()
            logger.info("live runner stopped")

    # ------------------------------------------------------------------
    def flatten(self, ctx: StrategyContext) -> None:
        for order in ctx.close_all():
            logger.info("flatten order submitted: {}", order.order_id)
