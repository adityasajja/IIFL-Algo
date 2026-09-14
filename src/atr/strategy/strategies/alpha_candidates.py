"""Three candidate edges for Indian equities, pre-registered before testing.

Why this file exists
--------------------
Everything active in this repo has now been measured and has failed against
buy-and-hold on 2020-2026 NSE data: the seven academic paper models, the
production entry rules, cross-sectional momentum, and the Episodic Pivot
(0 of 6 variants). So the honest question is not "does this idea work" but
"is there *anything* here that survives the same bar".

That question is only answerable if the candidate set is fixed **before** the
results are seen. Every extra idea tried raises the multiple-testing hurdle, and
the harness counts trials precisely so that searching harder cannot manufacture
a win. The three below are written down first, with their prior and their
falsification condition, and then scored.

Each is deliberately *long-only and near-fully-invested*. A long/flat timing
overlay starts structurally behind a benchmark that is itself always long, which
makes it a bad instrument for asking whether a signal has content. Keeping the
book invested means these compete with buy-and-hold on **selection and sizing**,
not on whether they happened to be in the market on the right days.

--------------------------------------------------------------------------
H1 — Short-horizon reversal
--------------------------------------------------------------------------
*Prior.* Lehmann (1990) and Jegadeesh (1990) documented short-horizon reversal
in US equities; it is one of the few cross-sectional effects that has survived
out of sample, and it is stronger in less liquid names where price pressure
decays slowly. This repo has an independent pointer to the same place: the
Episodic Pivot premise study found that Midcap 150 names returned **+0.97%** over
the 20 sessions after a gap-plus-volume catalyst against **+2.24%** for the
unconditional average. The crowd's arrival was followed by underperformance,
which is reversal.

*Rule.* Every ``hold`` sessions, rank the universe on trailing ``lookback``-day
return and buy the worst ``frac`` of it, equal-weighted. Long-only.

*Falsified if.* It does not beat both buy-and-hold and a matched random-selection
control on the same universe and cadence.

--------------------------------------------------------------------------
H2 — Volatility-managed exposure
--------------------------------------------------------------------------
*Prior.* Moreira & Muir (2017) show that scaling exposure by the inverse of
recent realised variance improves risk-adjusted returns, because volatility is
far more forecastable than return. Note what this is *not*: it makes no claim
about direction. It is a sizing rule.

*Rule.* Hold the universe equal-weighted, but multiply gross exposure by
``target_vol / realised_vol``, capped at 1.0 (no leverage). ``realised_vol`` is
the annualised 20-session standard deviation of the equal-weight universe index.

*Falsified if.* It does not improve Sharpe or Calmar against a control holding
the **same average exposure** — that control is the whole point, because a
lower-exposure book trivially has a lower drawdown and a lower return. Only the
timing of the exposure change is on trial.

--------------------------------------------------------------------------
H3 — Trend-filtered exposure
--------------------------------------------------------------------------
*Prior.* Time-series momentum (Moskowitz, Ooi & Pedersen 2012). Long history on
index futures; weaker on the underlying basket, where you pay the trading.

*Rule.* Hold the universe equal-weighted, flat when the equal-weight index is
below its ``sma``-session average.

*Falsified if.* It does not improve Sharpe or Calmar against a control that is
invested a **matched fraction of the time** at random. Beating buy-and-hold on
drawdown alone proves nothing — being out of the market half the time does that
by construction.

--------------------------------------------------------------------------
What would count as a win
--------------------------------------------------------------------------
Not "beats buy-and-hold on return". A long-only active rule that beats a
+96% buy-and-hold on 2020-2026 NSE large caps is a high bar and probably a
fluke if it happens once. The bar used here:

1. positive out-of-sample Sharpe,
2. beats the **matched control** by at least 2 standard deviations,
3. P(edge is real) >= 0.95 after deflating for every trial run,
4. and it beats buy-and-hold on **either** Sharpe or Calmar, stated explicitly.

A rule that fails (2) has demonstrated nothing beyond having been traded.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atr.strategy.base import Strategy

_EPS = 1e-9


def _universe_index(frames: dict[str, pd.DataFrame]) -> pd.Series:
    """Equal-weight index of the universe, built from daily returns.

    Constructed from returns rather than from a mean of price levels: the
    symbols in a feed do not all start at the same price, and averaging raw
    levels would weight a Rs 3,700 stock 37x a Rs 100 one. Returns are the only
    scale-free quantity here.
    """
    returns = pd.DataFrame(
        {symbol: pd.to_numeric(f["close"], errors="coerce").pct_change()
         for symbol, f in frames.items()}
    )
    equal_weight = returns.mean(axis=1).fillna(0.0)
    return (1.0 + equal_weight).cumprod()


def _finite(value) -> float:
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if np.isfinite(number) else 0.0


class _PortfolioStrategy(Strategy):
    """Shared machinery: a rebalance cadence and equal-weight targets."""

    name = "portfolio_strategy"

    #: Sessions between rebalances. Higher = less turnover, less tracking.
    rebalance_days: int = 10
    #: Never hold more than this many names, however the ranking comes out.
    max_positions: int = 40
    #: Gross exposure cap as a multiple of equity. The broker funds each order
    #: against total equity in isolation, so without this the book can reach
    #: N x equity — see `episodic_pivot.py` for the 3.49x that exposed it.
    max_gross_weight: float = 1.0

    def __init__(self, **params) -> None:
        super().__init__(**params)
        self._index: pd.Series | None = None
        self._last_rebalance: int | None = None

    # ------------------------------------------------------------------
    def prepare(self, frames: dict[str, pd.DataFrame]) -> None:
        self._index = _universe_index(frames)
        self._prepare_frames(frames)

    def _prepare_frames(self, frames: dict[str, pd.DataFrame]) -> None:
        """Hook for subclasses that need per-symbol columns."""

    # ------------------------------------------------------------------
    def _due(self, ctx) -> bool:
        if self._last_rebalance is None:
            return True
        return (ctx.index - self._last_rebalance) >= self.rebalance_days

    def _apply_weights(self, ctx, weights: dict[str, float], exposure: float) -> None:
        """Move the book to ``weights * exposure``, selling what is not wanted."""
        equity = ctx.equity
        if not np.isfinite(equity) or equity <= 0:
            return
        budget = self.max_gross_weight * exposure
        total = sum(weights.values())
        if total <= 0:
            for symbol in ctx.instruments:
                ctx.target(symbol, 0, tag=f"{self.name}-flat")
            return
        scale = budget / total

        for symbol in ctx.instruments:
            row = ctx.row(symbol)
            price = _finite(row.get("close"))
            if price <= 0:
                continue
            weight = weights.get(symbol, 0.0) * scale
            instrument = ctx.instruments[symbol]
            step = max(getattr(instrument, "quantity_step", 1) or 1, 1)
            qty = int((equity * weight / price) / step) * step
            ctx.target(symbol, qty, tag=f"{self.name}-rebalance")

    # ------------------------------------------------------------------
    def _ranked(self, ctx, column: str) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        for symbol in ctx.instruments:
            value = _finite(ctx.row(symbol).get(column))
            price = _finite(ctx.row(symbol).get("close"))
            if price > 0 and value == value and np.isfinite(value):
                out.append((symbol, value))
        return out


class ShortHorizonReversal(_PortfolioStrategy):
    """H1 — buy the worst performers, hold, repeat.

    ``exit_frac`` turns the plain rule into a **buffered** one: enter the worst
    ``frac`` of the universe, but only sell a name once it has left the worst
    ``exit_frac``. A rank portfolio without a buffer churns on noise — a name
    oscillating either side of the selection boundary is bought and sold
    repeatedly, and each round trip pays 0.28% in statutory costs. The buffer is
    the standard fix and it is mechanically motivated rather than fitted: it
    changes *when* a position is closed, never *which* names qualify.
    """

    name = "reversal_short_horizon"

    #: Trailing window used to rank.
    lookback: int = 20
    #: Fraction of the universe bought (the worst performers).
    frac: float = 0.10
    #: Fraction of the universe a held name must fall outside of before it is
    #: sold. Must be >= ``frac``; 0 disables the buffer (sell on every rebalance
    #: that the name is not re-selected).
    exit_frac: float = 0.0
    rebalance_days: int = 20
    max_positions: int = 40
    #: Ignore names whose price is below this — a penny stock's reversal is a
    #: microstructure artefact, not an edge.
    min_price: float = 20.0
    #: Control switch: pick the same number of names at the same cadence, but
    #: at random. Removes the ranking and nothing else.
    random_select: bool = False
    seed: int = 0

    def _prepare_frames(self, frames: dict[str, pd.DataFrame]) -> None:
        column = f"rev_{self.lookback}"
        for frame in frames.values():
            close = pd.to_numeric(frame["close"], errors="coerce")
            frame[column] = close / close.shift(self.lookback) - 1.0

    # ------------------------------------------------------------------
    def _select(self, ctx, ranked: list[tuple[str, float]]) -> list[str]:
        """Worst-first ranking → the names to hold."""
        target_n = max(1, min(int(len(ranked) * self.frac), self.max_positions))
        if self.exit_frac <= self.frac:
            return [symbol for symbol, _ in ranked[:target_n]]

        exit_n = max(target_n, min(int(len(ranked) * self.exit_frac), self.max_positions * 3))
        keep_set = {symbol for symbol, _ in ranked[:exit_n]}

        # Hold what is still bad enough, ranked worst-first so the trim below
        # keeps the worst rather than an arbitrary subset.
        survivors = [symbol for symbol, _ in ranked if symbol in keep_set
                     and ctx.has_position(symbol)][:target_n]

        vacancies = target_n - len(survivors)
        if vacancies <= 0:
            return survivors
        held = {symbol for symbol, _ in ranked if ctx.has_position(symbol)}
        fresh = [symbol for symbol, _ in ranked
                 if symbol not in held and symbol not in keep_set or
                 (symbol not in held and symbol in keep_set)]
        return survivors + [s for s in fresh if s not in survivors][:vacancies]

    # ------------------------------------------------------------------
    def on_bar(self, ctx) -> None:
        if not self._due(ctx):
            return
        column = f"rev_{self.lookback}"
        ranked = self._ranked(ctx, column)
        eligible = [
            pair for pair in ranked
            if _finite(ctx.row(pair[0]).get("close")) >= self.min_price
        ]
        if len(eligible) < 5:
            return
        self._last_rebalance = ctx.index

        eligible.sort(key=lambda pair: pair[1])           # worst first
        if self.random_select:
            import random

            rng = random.Random(self.seed * 100_003 + ctx.index)
            target_n = max(1, min(int(len(eligible) * self.frac), self.max_positions))
            chosen = rng.sample(eligible, target_n)
        else:
            chosen_symbols = set(self._select(ctx, eligible))
            chosen = [(s, v) for s, v in eligible if s in chosen_symbols]

        weights = {symbol: 1.0 for symbol, _ in chosen}
        self._apply_weights(ctx, weights, exposure=1.0)


class VolatilityManaged(_PortfolioStrategy):
    """H2 — hold everything, but size the book to a volatility target."""

    name = "vol_managed_exposure"

    target_vol: float = 15.0
    vol_window: int = 20
    rebalance_days: int = 5
    max_positions: int = 400
    #: Control switch: read the signal this many sessions away from the bar
    #: being traded. A circular shift preserves the exact sequence of exposure
    #: regimes — every run length, the same average exposure — and destroys only
    #: their alignment with returns. That is the whole null: same sizing, wrong
    #: dates.
    signal_shift: int = 0

    def _prepare_frames(self, frames: dict[str, pd.DataFrame]) -> None:
        index = self._index
        if index is None:
            return
        realised = index.pct_change().rolling(self.vol_window, min_periods=10).std()
        self._realised_vol = realised * np.sqrt(252.0) * 100.0

    def _exposure(self, ctx) -> float:
        series = getattr(self, "_realised_vol", None)
        if series is None:
            return 0.0
        position = (ctx.index - self.signal_shift) % len(series)
        vol = _finite(series.iloc[position])
        if vol <= 0:
            return 0.0
        return float(min(1.0, self.target_vol / vol))

    def on_bar(self, ctx) -> None:
        if not self._due(ctx):
            return
        self._last_rebalance = ctx.index
        exposure = self._exposure(ctx)
        weights = {
            symbol: 1.0
            for symbol in ctx.instruments
            if _finite(ctx.row(symbol).get("close")) > 0
        }
        if len(weights) > self.max_positions:
            weights = dict(list(weights.items())[: self.max_positions])
        self._apply_weights(ctx, weights, exposure=exposure)


class TrendFilteredExposure(_PortfolioStrategy):
    """H3 — hold everything, flat when the equal-weight index is below its SMA."""

    name = "trend_filtered_exposure"

    sma_window: int = 200
    rebalance_days: int = 5
    max_positions: int = 400
    #: Control switch — see `VolatilityManaged.signal_shift`.
    signal_shift: int = 0

    def _prepare_frames(self, frames: dict[str, pd.DataFrame]) -> None:
        index = self._index
        if index is None:
            return
        self._sma = index.rolling(self.sma_window, min_periods=self.sma_window // 2).mean()

    def _exposure(self, ctx) -> float:
        index = self._index
        sma = getattr(self, "_sma", None)
        if index is None or sma is None:
            return 0.0
        position = (ctx.index - self.signal_shift) % len(index)
        level = _finite(index.iloc[position])
        average = _finite(sma.iloc[position])
        if level <= 0 or average <= 0:
            return 0.0
        return 1.0 if level > average else 0.0

    def on_bar(self, ctx) -> None:
        if not self._due(ctx):
            return
        self._last_rebalance = ctx.index
        weights = {
            symbol: 1.0
            for symbol in ctx.instruments
            if _finite(ctx.row(symbol).get("close")) > 0
        }
        if len(weights) > self.max_positions:
            weights = dict(list(weights.items())[: self.max_positions])
        self._apply_weights(ctx, weights, exposure=self._exposure(ctx))
