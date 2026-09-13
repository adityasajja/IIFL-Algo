"""Walk-forward adapters for the academic paper models in ``atr.research.papers``.

Why this file exists
--------------------
The paper models could previously only emit *live* trade signals. They were
absent from ``atr.strategy.strategies.STRATEGIES``, so the walk-forward harness
could never score them — meaning their ``expected_value``,
``historical_win_rate`` and ``confidence_score`` were assumptions dressed up as
measurements (see the hardcoded ``win_rate = 0.5x`` constants in papers.py).

These adapters make every paper model scoreable by the same harness that gates
everything else: walk-forward folds, deflated Sharpe, buy-and-hold benchmark,
and a random-selection control. A model earns the right to be trusted here,
not from its citation.
"""

from __future__ import annotations

import pandas as pd

from atr.research.papers import PaperAlphaSignal, evaluate_one
from atr.strategy.base import Strategy


class PaperAlphaBase(Strategy):
    """Run one named paper model per bar and translate its signal to a target.

    Subclasses only need to set ``paper_id`` (the ``strategy_id`` emitted by
    papers.py) and ``name`` (how it appears in ``/strategies``).
    """

    #: strategy_id emitted by the corresponding function in papers.py.
    paper_id: str = ""
    name: str = "paper_alpha"

    #: Fraction of equity deployed per position.
    allocation: float = 0.20
    #: Skip signals below this confidence. Raising it does not make the model
    #: better — it just trades less, and the harness will say so.
    min_confidence: float = 0.0
    #: Bars of history handed to the model. Generous enough for the 200-SMA
    #: and 6-month lookbacks several models need.
    lookback: int = 300

    def on_bar(self, ctx) -> None:
        for symbol in ctx.instruments:
            df = ctx.history(symbol, n=self.lookback)
            if df is None or len(df) < 60:
                continue

            row = ctx.row(symbol)
            # Use `.get` — NOT `"close" in row`. `Row` subclasses tuple and only
            # overrides `get`/`__getitem__`, so `in` falls through to
            # `tuple.__contains__` and tests *values*, not column names. It is
            # therefore always False, which silently skipped every signal and
            # produced a clean "0 trades, no edge" that was really "no data".
            price = row.get("close")
            if price is None or pd.isna(price) or price <= 0:
                continue

            signal = self._evaluate(symbol, df)
            if signal is None:
                ctx.target(symbol, 0, tag="no-signal")
                continue
            if signal.confidence_score < self.min_confidence:
                ctx.target(symbol, 0, tag="below-confidence")
                continue

            qty = self._size(ctx, symbol, float(price))
            if qty:
                ctx.target(symbol, qty, tag=self.paper_id or "paper")

    # ------------------------------------------------------------------
    def _evaluate(self, symbol: str, df: pd.DataFrame) -> PaperAlphaSignal | None:
        """Return this adapter's signal, if the model fired.

        Runs only *this* adapter's model. The batch evaluator would run all
        seven and throw six away, which inside a per-bar × per-symbol loop is
        the difference between a sweep finishing and a timeout.
        """
        try:
            return evaluate_one(self.paper_id, symbol, df)
        except Exception:  # noqa: BLE001 — one bad symbol must not kill a fold
            return None

    def _size(self, ctx, symbol: str, price: float) -> int:
        instrument = ctx.instruments[symbol]
        notional = ctx.equity * self.allocation
        step = max(getattr(instrument, "quantity_step", 1) or 1, 1)
        return int(int(notional / max(price * instrument.multiplier, 1e-9) / step) * step)


# ─── International models ────────────────────────────────────────────────────


class JegadeeshTitmanMomentum(PaperAlphaBase):
    """Jegadeesh & Titman (1993, 2001) cross-sectional momentum."""

    name = "paper_jegadeesh_titman"
    paper_id = "momentum_jegadeesh_titman"


class AvellanedaLeeMeanReversion(PaperAlphaBase):
    """Avellaneda & Lee (2010) statistical mean reversion / Bollinger z-score."""

    name = "paper_avellaneda_lee"
    paper_id = "stat_arb_avellaneda_lee"


class VolatilityBreakout(PaperAlphaBase):
    """Donchian / Kaufman volatility-expansion breakout."""

    name = "paper_volatility_breakout"
    paper_id = "volatility_breakout"


class MultiFactorComposite(PaperAlphaBase):
    """Composite of trend, volatility compression and volume flow."""

    name = "paper_multi_factor_composite"
    paper_id = "multi_factor_composite"


# ─── India-specific models ───────────────────────────────────────────────────


class IimaNseMomentum(PaperAlphaBase):
    """IIMA / NSE intermediate-horizon momentum study."""

    name = "paper_iima_nse_momentum"
    paper_id = "iima_nse_momentum"


class Nism52wHighProximity(PaperAlphaBase):
    """NISM 52-week-high proximity breakout."""

    name = "paper_nism_52w_high"
    paper_id = "nism_52w_high_proximity"


class SehgalLowVolAnomaly(PaperAlphaBase):
    """Sehgal low-volatility anomaly (Indian market)."""

    name = "paper_sehgal_low_vol"
    paper_id = "sehgal_low_vol_anomaly"
