"""SMA crossover — the reference implementation for this engine.

A trend-following baseline on cash equities. It exists to prove the plumbing
works and to give you something to beat, not because it has an edge.
"""

from __future__ import annotations

import pandas as pd

from atr.core.enums import AssetClass
from atr.strategy.base import Strategy
from atr.strategy.indicators import sma


class SmaCrossover(Strategy):
    name = "sma_crossover"

    fast: int = 20
    slow: int = 50
    #: Fraction of equity deployed per position.
    allocation: float = 0.20
    atr_stop_multiple: float | None = None

    def prepare(self, frames: dict[str, pd.DataFrame]) -> None:
        for frame in frames.values():
            frame["sma_fast"] = sma(frame["close"], self.fast)
            frame["sma_slow"] = sma(frame["close"], self.slow)
            # Previous bar's averages, shifted here rather than read back with
            # ctx.history() inside on_bar — a frame slice per bar per symbol
            # is one of the most expensive things a strategy can do.
            frame["sma_fast_prev"] = frame["sma_fast"].shift(1)
            frame["sma_slow_prev"] = frame["sma_slow"].shift(1)

    def on_bar(self, ctx) -> None:
        for symbol in ctx.instruments:
            row = ctx.row(symbol)
            fast_now, slow_now = row["sma_fast"], row["sma_slow"]
            if pd.isna(fast_now) or pd.isna(slow_now):
                continue

            fast_prev, slow_prev = row["sma_fast_prev"], row["sma_slow_prev"]
            # First bar of the series has no previous value: nothing to cross.
            if pd.isna(fast_prev) or pd.isna(slow_prev):
                continue

            price = row["close"]
            if not price or pd.isna(price):
                continue

            target_qty = self._size(ctx, symbol, price)
            if target_qty == 0:
                continue

            long_signal = fast_now > slow_now and fast_prev <= slow_prev
            short_signal = fast_now < slow_now and fast_prev >= slow_prev

            if long_signal:
                ctx.target(symbol, target_qty, tag="long")
            elif short_signal:
                if ctx.instruments[symbol].asset_class is AssetClass.EQUITY:
                    ctx.target(symbol, 0, tag="exit-short-signal")
                else:
                    ctx.target(symbol, -target_qty, tag="short")

    def describe_signal(self, symbol: str, ctx) -> str | None:
        """Report the crossover state, so a trade can say why it opened.

        Only called on a bar where the wrapper is about to record an entry, so
        it does not need to be cheap for every symbol on every bar — but it is
        written against the current row anyway, so it can never describe a
        different bar than the one the order is placed on.
        """
        row = ctx.row(symbol)
        fast, slow = row["sma_fast"], row["sma_slow"]
        if pd.isna(fast) or pd.isna(slow):
            return None
        fast_prev, slow_prev = row["sma_fast_prev"], row["sma_slow_prev"]
        close = row["close"]

        if not pd.isna(fast_prev) and fast > slow and fast_prev <= slow_prev:
            return (
                f"SMA {self.fast} ({fast:,.2f}) crossed above "
                f"SMA {self.slow} ({slow:,.2f}) with close at {close:,.2f}"
            )
        if not pd.isna(fast_prev) and fast < slow and fast_prev >= slow_prev:
            return (
                f"SMA {self.fast} ({fast:,.2f}) crossed below "
                f"SMA {self.slow} ({slow:,.2f}) with close at {close:,.2f}"
            )
        return None

    def _size(self, ctx, symbol: str, price: float) -> int:
        instrument = ctx.instruments[symbol]
        notional = ctx.equity * self.allocation
        step = max(instrument.quantity_step, 1)
        qty = int(notional / max(price * instrument.multiplier, 1e-9) / step) * step
        return int(qty)
