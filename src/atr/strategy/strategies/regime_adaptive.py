"""Regime-Adaptive Multi-Strategy Meta-Selector.

Uses Laya (`convaiinnovations/laya`) to evaluate market conditions (volatility,
trend persistence, momentum) and dynamically route capital to the optimal strategy:

- "TRENDING_BULLISH": Allocate to Momentum / Trend following (e.g. SmaCrossover, MomentumBreakout).
- "TRENDING_BEARISH": Defensive / Short exposure if permitted, else stay flat / cash.
- "MEAN_REVERTING": Allocate to mean-reversion or tight oscillating strategies.
- "HIGH_VOLATILITY": Defensive cash preservation or volatility breakout with reduced sizing.
"""

from __future__ import annotations

import pandas as pd

from atr.strategy.base import Strategy
from atr.strategy.indicators import atr, rsi, sma
from atr.strategy.regime_laya import get_regime_classifier


class RegimeAdaptiveStrategy(Strategy):
    name = "regime_adaptive"

    fast: int = 20
    slow: int = 50
    adx_period: int = 14
    atr_period: int = 14
    allocation: float = 0.25
    eval_interval: int = 5  # Evaluate regime every N bars to avoid excessive reclassification

    def __init__(self, **params) -> None:
        super().__init__(**params)
        self.classifier = get_regime_classifier()
        self._regime_cache: dict[str, str] = {}
        self._bar_count: dict[str, int] = {}

    def prepare(self, frames: dict[str, pd.DataFrame]) -> None:
        for frame in frames.values():
            frame["sma_fast"] = sma(frame["close"], self.fast)
            frame["sma_slow"] = sma(frame["close"], self.slow)
            frame["sma_ratio"] = frame["sma_fast"] / frame["sma_slow"]
            frame["atr"] = atr(frame["high"], frame["low"], frame["close"], self.atr_period)
            frame["atr_pct"] = (frame["atr"] / frame["close"]) * 100.0
            frame["rsi"] = rsi(frame["close"], 14)
            frame["ret_20d"] = frame["close"].pct_change(20) * 100.0

            # Previous bar averages for crossovers
            frame["sma_fast_prev"] = frame["sma_fast"].shift(1)
            frame["sma_slow_prev"] = frame["sma_slow"].shift(1)

    def on_bar(self, ctx) -> None:
        equity = ctx.equity
        for symbol in ctx.instruments:
            row = ctx.row(symbol)
            fast_now, slow_now = row["sma_fast"], row["sma_slow"]
            if pd.isna(fast_now) or pd.isna(slow_now):
                continue

            price = row["close"]
            if not price or pd.isna(price):
                continue

            count = self._bar_count.get(symbol, 0)
            self._bar_count[symbol] = count + 1

            # Periodically evaluate market regime via Laya decision model
            if symbol not in self._regime_cache or (count % self.eval_interval == 0):
                atr_pct = float(row["atr_pct"]) if not pd.isna(row["atr_pct"]) else 1.5
                rsi_val = float(row["rsi"]) if not pd.isna(row["rsi"]) else 50.0
                ret_20d = float(row["ret_20d"]) if not pd.isna(row["ret_20d"]) else 0.0
                ratio = float(row["sma_ratio"]) if not pd.isna(row["sma_ratio"]) else 1.0

                verdict = self.classifier.classify_market_context(
                    symbol=symbol,
                    atr_pct=atr_pct,
                    adx=28.0 if ratio > 1.01 or ratio < 0.99 else 15.0,
                    rsi=rsi_val,
                    return_20d=ret_20d,
                    sma_ratio=ratio,
                )
                self._regime_cache[symbol] = verdict.regime

            current_regime = self._regime_cache[symbol]

            # Route actions based on active market regime
            fast_prev, slow_prev = row["sma_fast_prev"], row["sma_slow_prev"]
            if pd.isna(fast_prev) or pd.isna(slow_prev):
                continue

            target_qty = self._size(ctx, symbol, price, equity)
            if target_qty == 0:
                continue

            if current_regime == "TRENDING_BULLISH":
                # Favor trend entries
                if fast_now > slow_now and fast_prev <= slow_prev:
                    ctx.target(symbol, target_qty, tag="laya-bullish-trend")
            elif current_regime in ("HIGH_VOLATILITY", "TRENDING_BEARISH"):
                # Exit equity positions or protect capital
                pos = ctx.portfolio.positions.get(symbol)
                if pos and pos.quantity > 0:
                    ctx.target(symbol, 0, tag=f"laya-exit-{current_regime.lower()}")
            elif current_regime == "MEAN_REVERTING":
                # Range-bound regime: buy oversold, take profits on overbought
                rsi_val = row["rsi"]
                if not pd.isna(rsi_val):
                    if rsi_val < 32:
                        ctx.target(symbol, int(target_qty * 0.7), tag="laya-oversold-reversion")
                    elif rsi_val > 68:
                        ctx.target(symbol, 0, tag="laya-overbought-exit")

    def describe_signal(self, symbol: str, ctx) -> str | None:
        regime = self._regime_cache.get(symbol, "UNKNOWN")
        return f"Laya Market Regime: {regime}"
