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
from datetime import datetime, timedelta
from datetime import time as dtime

import pandas as pd
from loguru import logger

from atr.backtest.portfolio import Portfolio
from atr.brokers.base import Broker
from atr.brokers.iifl.auth import IST, Session
from atr.brokers.iifl.feeds import IiflLiveFeed
from atr.core.models import Instrument
from atr.execution.risk import RiskEngine, RiskLimits
from atr.strategy.base import Strategy, StrategyContext

OHLCV = ["open", "high", "low", "close", "volume"]


@dataclass
class LiveConfig:
    freq: str = "1min"
    poll_timeout: float = 1.0
    square_off_time: dtime = dtime(15, 15)
    #: Bars of history fetched before the first live bar, so indicators are
    #: valid at the open. 0 skips warmup, leaving indicators NaN until enough
    #: live bars accumulate.
    warmup_bars: int = 500
    #: Hard cap on retained bars per symbol — bounds memory over a long session.
    max_history_bars: int = 2_000
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
        client=None,
        feed=None,
    ) -> None:
        self.session = session
        self.instruments = instruments
        self.strategy = strategy
        self.broker = broker
        self.config = config or LiveConfig()
        self.portfolio = Portfolio(initial_cash=initial_cash)
        self.risk = RiskEngine(risk or RiskLimits())
        self.frames: dict[str, pd.DataFrame] = warmup_frames or {}
        # Injectable so the loop can be tested without the live bridge.
        self.feed = (
            feed if feed is not None
            else IiflLiveFeed(session, instruments, freq=self.config.freq)
        )
        self.trades: list = []
        self._stop = False
        self._squared_off_date: datetime.date | None = None
        self._client = client
        self._indicator_len = -1

    # ------------------------------------------------------------------
    @staticmethod
    def _new_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=OHLCV)

    def _length(self) -> int:
        return len(next(iter(self.frames.values()))) if self.frames else 0

    def _ensure_frames(self) -> None:
        for symbol in self.instruments:
            self.frames.setdefault(symbol, self._new_frame())

    def load_warmup(self) -> None:
        """Fetch recent candles so indicators are valid from the first live bar.

        Without this, ``prepare()`` runs against an empty frame, every indicator
        stays NaN, and the strategy never signals at all — a silent no-op rather
        than a visible failure.
        """
        bars = self.config.warmup_bars
        if bars <= 0 or any(not f.empty for f in self.frames.values()):
            return
        client = self._client or self._settings_client()
        if client is None:
            logger.warning("no client available — skipping warmup, indicators start cold")
            return

        from atr.brokers.iifl.feeds import IiflHistoricalFeed

        # Roughly 375 one-minute bars per NSE session; allow for weekends.
        to_date = datetime.now(IST)
        from_date = to_date - timedelta(days=max(5, bars // 300 + 4))
        try:
            frame = IiflHistoricalFeed(
                client,
                self.instruments,
                interval=self.config.freq,
                from_date=from_date,
                to_date=to_date,
            ).fetch()
        except Exception as exc:  # noqa: BLE001 - warmup is best-effort
            logger.warning("warmup fetch failed, starting cold: {}", exc)
            return
        if frame.empty:
            logger.warning("warmup fetch returned no candles, starting cold")
            return

        index = pd.DatetimeIndex(sorted(frame["ts"].unique()))
        for symbol in self.instruments:
            sub = frame[frame["symbol"] == symbol].set_index("ts")
            self.frames[symbol] = sub.reindex(index)[OHLCV].tail(bars)
        logger.info(
            "warmup: {} bars x {} symbols ({} -> {})",
            self._length(), len(self.instruments), index[0], index[-1],
        )

    def _settings_client(self):
        """Build a REST client from settings so warmup works out of the box."""
        try:
            from atr.brokers.iifl.client import IiflClient
            from atr.config.settings import get_settings

            settings = get_settings()
            client = IiflClient(
                app_key=settings.iifl_app_key, app_secret=settings.iifl_app_secret
            )
            client.set_session(self.session)
            return client
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not build client for warmup: {}", exc)
            return None

    def _append_snapshot(self, snapshot) -> None:
        """Append one row per instrument, NaN where the symbol had no bar.

        Aligning every symbol onto the same grid is what the backtester does
        (``BacktestEngine._build_frames``), and it is what makes a single
        ``ctx.index`` meaningful — with ragged frames, one index cannot address
        them all and ``ctx.row()`` silently returns the wrong bar.
        """
        index = pd.DatetimeIndex([snapshot.ts])
        for symbol in self.instruments:
            bar = snapshot.bars.get(symbol)
            values = (
                [[bar.open, bar.high, bar.low, bar.close, bar.volume]]
                if bar is not None
                else [[float("nan")] * len(OHLCV)]
            )
            row = pd.DataFrame(values, index=index, columns=OHLCV)
            self.frames[symbol] = pd.concat([self.frames[symbol], row])
        self._trim()

    def _trim(self) -> None:
        cap = self.config.max_history_bars
        if cap <= 0:
            return
        for symbol, frame in self.frames.items():
            if len(frame) > cap:
                self.frames[symbol] = frame.iloc[-cap:]

    def _refresh_indicators(self) -> None:
        """Re-run ``prepare()`` so the newest bar has indicator values.

        ``prepare()`` is a one-shot vectorised pass over a static frame in
        backtests. Live frames grow, so calling it once at startup leaves every
        subsequent bar with NaN indicators — which is why this runner could
        never actually trade.
        """
        length = self._length()
        if length == self._indicator_len:
            return
        self.strategy.prepare(self.frames)
        self._indicator_len = length

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
        self.load_warmup()
        self._ensure_frames()
        self._refresh_indicators()
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

                self._append_snapshot(snapshot)
                self._refresh_indicators()
                for symbol, bar in snapshot.bars.items():
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
                ctx.index = max(self._length() - 1, 0)
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
