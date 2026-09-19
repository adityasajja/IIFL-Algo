"""Momentum breakout — follows through on a new high with volume confirmation.

Enters long when price breaks above the N-bar high with above-average volume,
confirming real accumulation rather than a dry breakout. Exits are handled
within the strategy itself via a fixed stop-loss, a profit target, and a
trailing stop that locks in gains on sustained moves.

This is a single-strategy baseline for equity trading — one instrument at a
time, long-only (equities cannot be shorted without a separate mechanism).
"""

from __future__ import annotations

import pandas as pd

from atr.core.enums import AssetClass
from atr.strategy.base import Strategy


class MomentumBreakout(Strategy):
    name = "momentum_breakout"

    lookahead: int = 20
    volume_multiple: float = 1.5
    min_bars: int = 30
    allocation: float = 0.20
    trend_filter: int | None = 100
    stop_loss_pct: float = 3.0
    take_profit_pct: float = 6.0
    trailing_stop_pct: float = 2.5
    #: Bars to wait after an exit before re-entering the same symbol.
    cooldown_bars: int = 5
    #: Consecutive closes above the lookback high required for entry.
    consecutive_breakout: int = 2

    def __init__(self, **params) -> None:
        super().__init__(**params)
        self._entry_price: dict[str, float] = {}
        self._peak_price: dict[str, float] = {}
        self._cooldown: dict[str, int] = {}

    # ------------------------------------------------------------------
    def prepare(self, frames: dict[str, pd.DataFrame]) -> None:
        for frame in frames.values():
            # Use the close for the lookback high so a breakout is "close above
            # the highest close of the last N bars" — this is the standard
            # momentum-breakout signal and avoids the close-vs-high ceiling in
            # low-range bars where high ≈ close * 1.01.
            frame["lookback_high"] = frame["close"].rolling(self.lookahead, min_periods=self.lookahead).max()
            frame["avg_volume"] = frame["volume"].rolling(self.lookahead, min_periods=self.lookahead).mean()
            frame["lookback_high_prev"] = frame["lookback_high"].shift(1)
            if self.trend_filter:
                frame["trend_sma"] = (
                    frame["close"].rolling(self.trend_filter, min_periods=self.trend_filter).mean()
                )
            frame["breakout"] = frame["close"] > frame["lookback_high_prev"]
            frame["consecutive_breakout"] = (
                frame["breakout"]
                .astype(int)
                .groupby((~frame["breakout"]).cumsum())
                .cumsum()
            )

    # ------------------------------------------------------------------
    def on_bar(self, ctx) -> None:
        for symbol in ctx.instruments:
            if ctx.instruments[symbol].asset_class is not AssetClass.EQUITY:
                continue

            row = ctx.row(symbol)

            if ctx.index < self.min_bars:
                continue

            # Cooldown countdown
            if symbol in self._cooldown:
                if self._cooldown[symbol] > 0:
                    self._cooldown[symbol] -= 1
                    continue
                del self._cooldown[symbol]

            close = row["close"]
            high = row["high"]
            low = row.get("low")
            if not close or pd.isna(close) or pd.isna(high) or pd.isna(low):
                continue

            sym_lookback_prev = row.get("lookback_high_prev")
            consecutive = row.get("consecutive_breakout") or 0
            if pd.isna(sym_lookback_prev) or pd.isna(consecutive):
                continue

            has_pos = ctx.has_position(symbol)
            entry_px = self._entry_price.get(symbol)

            # --- Exits ---
            if has_pos and entry_px is not None:
                peak = self._peak_price.get(symbol, close)
                peak = max(peak, high)

                stop_price = entry_px * (1 - self.stop_loss_pct / 100.0)
                target_price = entry_px * (1 + self.take_profit_pct / 100.0)
                trailing_price = peak * (1 - self.trailing_stop_pct / 100.0)

                # Fixed stop-loss or trailing stop triggered
                if low <= stop_price or low <= trailing_price:
                    ctx.target(symbol, 0, tag="exit-stop-loss")
                    self._entry_price.pop(symbol, None)
                    self._peak_price.pop(symbol, None)
                    self._cooldown[symbol] = self.cooldown_bars
                    continue

                # Take-profit
                if high >= target_price:
                    ctx.target(symbol, 0, tag="exit-take-profit")
                    self._entry_price.pop(symbol, None)
                    self._peak_price.pop(symbol, None)
                    self._cooldown[symbol] = self.cooldown_bars
                    continue

                self._peak_price[symbol] = peak
                continue

            # --- Entry ---
            avg_vol = row.get("avg_volume")
            vol = row.get("volume")
            vol_ok = True
            if avg_vol is not None and not pd.isna(avg_vol) and vol is not None:
                vol_ok = vol > avg_vol * self.volume_multiple

            if self.trend_filter:
                trend = row.get("trend_sma")
                if pd.isna(trend) or close < trend:
                    continue

            # Sustained breakout: N consecutive closes above the lookback high
            if consecutive >= self.consecutive_breakout and vol_ok:
                target_qty = self._size(ctx, symbol, close)
                if target_qty > 0:
                    ctx.target(symbol, target_qty, tag="momentum-entry")
                    self._entry_price[symbol] = close
                    self._peak_price[symbol] = close
                    self._cooldown.pop(symbol, None)

    # ------------------------------------------------------------------
    def on_stop(self, ctx) -> None:
        ctx.close_all()

    # ------------------------------------------------------------------
    def describe_signal(self, symbol: str, ctx) -> str | None:
        if ctx.index < self.min_bars:
            return None
        row = ctx.row(symbol)
        close = row["close"]
        lookback_prev = row.get("lookback_high_prev")
        consecutive = row.get("consecutive_breakout") or 0
        if pd.isna(lookback_prev) or pd.isna(consecutive) or not close:
            return None

        if consecutive >= self.consecutive_breakout and not ctx.has_position(symbol):
            return (
                f"Close {close:,.2f} sustained above {self.lookahead}-bar high "
                f"({lookback_prev:,.2f}) — {int(consecutive)} consecutive closes"
            )
        return None

    # ------------------------------------------------------------------
    def _size(self, ctx, symbol: str, price: float) -> int:
        instrument = ctx.instruments[symbol]
        notional = ctx.equity * self.allocation
        step = max(instrument.quantity_step, 1)
        qty = int(notional / max(price * instrument.multiplier, 1e-9) / step) * step
        return int(qty)
