"""Opening Range Breakout — built for index F&O.

The first ``orb_minutes`` of the session define a range. A break of that range
with an ATR-based stop is the entry; everything is flattened by ``exit_time``
so no overnight gap risk is carried.

This is the template to copy if you trade NSE F&O: it shows contract sizing
(lot-aware), intraday square-off, and stop placement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pandas as pd

from atr.core.enums import OrderType, Side, TimeInForce
from atr.strategy.base import Strategy
from atr.strategy.indicators import atr, session_range


@dataclass
class _State:
    day: object = None
    entered: bool = False
    stop: float | None = None
    direction: int = 0


class OpeningRangeBreakout(Strategy):
    name = "opening_range_breakout"

    orb_minutes: int = 15
    exit_time: time = time(15, 15)
    atr_window: int = 14
    atr_stop_multiple: float = 2.0
    risk_per_trade_pct: float = 0.5  # % of equity risked per trade
    lots: int = 1

    def __init__(self, **params) -> None:
        super().__init__(**params)
        self._state: dict[str, _State] = {}

    # ------------------------------------------------------------------
    def prepare(self, frames: dict[str, pd.DataFrame]) -> None:
        for frame in frames.values():
            orb_high, orb_low = session_range(frame, self.orb_minutes)
            frame["orb_high"] = orb_high
            frame["orb_low"] = orb_low
            frame["atr"] = atr(frame["high"], frame["low"], frame["close"], self.atr_window)

    # ------------------------------------------------------------------
    def on_bar(self, ctx) -> None:
        for symbol in ctx.instruments:
            row = ctx.row(symbol)
            if pd.isna(row.get("orb_high")) or pd.isna(row.get("atr")):
                continue
            price = row["close"]
            if not price or pd.isna(price):
                continue

            state = self._state.setdefault(symbol, _State())
            today = ctx.now.date() if ctx.now else None
            if state.day != today:
                state.day = today
                state.entered = False
                state.stop = None
                state.direction = 0

            now_time = ctx.now.time() if ctx.now else None

            # 1. Mandatory intraday square-off.
            if now_time and now_time >= self.exit_time:
                if ctx.has_position(symbol):
                    ctx.close(symbol, tag="eod-squareoff")
                state.entered = False
                continue

            # 2. Self-heal: an entry order can be rejected by risk or margin.
            #    Without this the strategy would sit out the rest of the day
            #    believing it was in a trade it never got.
            if state.entered and not ctx.has_position(symbol):
                state.entered = False
                state.stop = None
                state.direction = 0

            # 3. Stop management comes before entries.
            if ctx.has_position(symbol) and state.stop is not None:
                hit = (
                    price <= state.stop if state.direction > 0 else price >= state.stop
                )
                if hit:
                    ctx.close(symbol, tag="stop")
                    state.entered = False
                    state.stop = None
                    continue

            if state.entered:
                continue

            # 4. Only consider entries once the opening range is complete.
            if now_time is None or now_time < self._window_end(row):
                continue

            position_size = self._size(ctx, symbol, row)
            if position_size <= 0:
                continue

            if price > row["orb_high"]:
                self._enter(ctx, symbol, Side.BUY, position_size, row, state)
            elif price < row["orb_low"]:
                self._enter(ctx, symbol, Side.SELL, position_size, row, state)

    # ------------------------------------------------------------------
    def _window_end(self, row) -> time:
        """NSE continuous trading starts 09:30, so a 15-min OR completes 09:45."""
        end = datetime(2000, 1, 1, 9, 30) + timedelta(minutes=self.orb_minutes)
        return end.time()

    def _size(self, ctx, symbol: str, row) -> int:
        instrument = ctx.instruments[symbol]
        stop_distance = (row["atr"] or 0) * self.atr_stop_multiple
        if stop_distance <= 0:
            return 0
        risk_budget = ctx.equity * (self.risk_per_trade_pct / 100)
        per_unit_risk = stop_distance * instrument.multiplier
        if per_unit_risk <= 0:
            return 0
        qty = int(risk_budget / per_unit_risk)
        step = max(int(instrument.quantity_step or 1), 1)
        qty = (qty // step) * step
        return max(qty, 0)

    def _enter(self, ctx, symbol: str, side: Side, qty: int, row, state: _State) -> None:
        signed = qty if side is Side.BUY else -qty
        order = ctx.order(
            symbol,
            signed,
            order_type=OrderType.MARKET,
            tif=TimeInForce.DAY,
            tag="orb-entry",
        )
        if order is not None:
            stop_distance = (row["atr"] or 0) * self.atr_stop_multiple
            state.entered = True
            state.direction = side.sign
            state.stop = (
                row["close"] - stop_distance if side is Side.BUY else row["close"] + stop_distance
            )
