"""Cross-sectional relative strength.

A structurally different hypothesis from the per-symbol entry rules.

Those rules asked "is this stock a good buy right now?" — a timing question,
and on liquid large caps the answer is almost always "it was better to just
hold". This asks a *ranking* question instead: of everything available, which
names have been strongest, and hold those. Momentum is documented
cross-sectionally (Jegadeesh & Titman) and the mechanism is different: you are
not predicting direction, you are rotating capital toward whatever is already
working and letting the ranking do the work.

The skip month matters. Short-horizon returns mean-revert, so momentum is
measured over months 2-12 and the most recent month is deliberately excluded —
including it tends to flip the sign of the effect.
"""

from __future__ import annotations

import math

from loguru import logger

from atr.strategy.base import Strategy
from atr.strategy.indicators import sma

#: Momentum column, in trading days. 126 ~ 6 months, 252 ~ 1 year.
_PRE_MOMENTUM = "_mom{}_skip{}"
_PRE_TREND = "_sma{}"


class CrossSectionalMomentum(Strategy):
    """Hold the top-N ranked names, rebalanced on a fixed cadence."""

    name = "cross_sectional_momentum"

    def __init__(self, **params) -> None:
        super().__init__(**params)
        #: Momentum measurement window, in bars.
        self.lookback = int(params.get("lookback", 126))
        #: Bars to skip before the measurement window (short-term reversal).
        self.skip = int(params.get("skip", 21))
        #: How many names to hold.
        self.top_n = int(params.get("top_n", 5))
        #: Bars between rebalances. 21 ~ monthly.
        self.rebalance_days = int(params.get("rebalance_days", 21))
        #: Optional per-name trend filter: only buy above this moving average.
        #: 0 disables. This is a risk control, not a return source.
        self.trend_sma = int(params.get("trend_sma", 0))
        self.allocation = float(params.get("allocation", 1.0 / max(self.top_n, 1)))
        #: ``"momentum"`` ranks by trailing return. ``"random"`` picks the same
        #: number of names at random and is the control that decides whether the
        #: ranking contributes anything at all — if random matches momentum, the
        #: signal is just concentration, not selection.
        self.ranking = str(params.get("ranking", "momentum"))
        self.seed = int(params.get("seed", 7))
        self._last_rebalance: int | None = None

    # ------------------------------------------------------------------
    def prepare(self, frames) -> None:
        for frame in frames.values():
            frame[self._momentum_column()] = (
                frame["close"].shift(self.skip)
                / frame["close"].shift(self.skip + self.lookback)
                - 1.0
            )
            if self.trend_sma:
                frame[_PRE_TREND.format(self.trend_sma)] = sma(frame["close"], self.trend_sma)

    def _momentum_column(self) -> str:
        return _PRE_MOMENTUM.format(self.lookback, self.skip)

    # ------------------------------------------------------------------
    def on_bar(self, ctx) -> None:
        if ctx.index < self.skip + self.lookback:
            return
        if self._last_rebalance is not None and ctx.index - self._last_rebalance < self.rebalance_days:
            return
        self._last_rebalance = ctx.index

        column = self._momentum_column()
        scored: list[tuple[float, str]] = []
        for symbol in ctx.instruments:
            row = ctx.row(symbol)
            try:
                momentum = row[column]
                price = row["close"]
            except KeyError:
                continue
            if momentum is None or price is None:
                continue
            if not (math.isfinite(float(momentum)) and math.isfinite(float(price))):
                continue
            if float(price) <= 0:
                continue
            if self.trend_sma:
                line = row[_PRE_TREND.format(self.trend_sma)]
                if line is None or not math.isfinite(float(line)) or float(price) <= float(line):
                    continue
            scored.append((float(momentum), symbol))

        if not scored:
            logger.debug("no symbols scored on bar {}", ctx.index)
            return

        if self.ranking == "random":
            import numpy as np

            np.random.default_rng(self.seed + ctx.index).shuffle(scored)
        else:
            scored.sort(reverse=True)
        targets = {symbol for _, symbol in scored[: self.top_n]}

        # Rotate out first, so capital is free for the incoming names.
        for symbol in ctx.instruments:
            if symbol not in targets and not ctx.position(symbol).is_flat:
                ctx.close(symbol, tag="rotate-out")

        for symbol in sorted(targets):
            if not ctx.position(symbol).is_flat:
                continue
            price = float(ctx.row(symbol)["close"])
            if price <= 0:
                continue
            quantity = int((ctx.equity * self.allocation) / price)
            if quantity > 0:
                ctx.order(symbol, quantity, tag="rotate-in")
