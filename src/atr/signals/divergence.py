"""RSI divergence — where momentum and price disagree about a swing.

Bullish divergence: price makes a lower swing low, but RSI at that same low
is *higher* than RSI at the previous low — the selling made a new price low
without the momentum to back it up. Bearish divergence is the mirror at
swing highs. Read off the two most recent same-kind swings only (the
classic, two-point definition), built on `atr.signals.swing_points` the same
way every other pattern here is.

Same posture as `atr.signals.chart_patterns`: a shape flag on the *current*
bar, not a promise the price reverses — divergence can (and does) persist or
extend before anything reverses.
"""

from __future__ import annotations

import pandas as pd

from atr.signals.swing_points import SwingPoint, recent_swings
from atr.strategy.indicators import rsi as _rsi_indicator


def detect_rsi_divergence(
    frame: pd.DataFrame,
    *,
    window: int = 3,
    lookback: int = 100,
    rsi_period: int = 14,
    extreme_threshold: float = 35.0,
    min_rsi_gap: float = 5.0,
) -> dict[str, float]:
    """``bullish_divergence``/``bearish_divergence`` (0.0/1.0) on the latest bar.

    True only when the more recent of the two swings is at (or is) the
    latest bar in `frame` — divergence found two weeks ago and never
    resolved is not a signal on today's bar.

    RSI is bounded and mean-reverting, so a bare "RSI is higher at the
    second low" test fires on almost any sustained trend with minor bounces
    in it — RSI just doesn't make unboundedly lower lows the way price can,
    so the *first* of two lows in a steady downtrend is very often the more
    extreme reading by construction, not because momentum actually turned.
    Two guards keep this to the cases that mean something:

    * `extreme_threshold` — the *prior* swing's RSI must itself have been in
      oversold (bullish, ``<= threshold``) or overbought (bearish,
      ``>= 100 - threshold``) territory. Divergence is a warning about
      exhaustion; two swings in the middle of the range agreeing to
      disagree by a point or two isn't that.
    * `min_rsi_gap` — the two readings must differ by at least this many
      points, not just any positive difference the smoothing happened to
      produce.
    """
    out = {"bullish_divergence": 0.0, "bearish_divergence": 0.0}
    if frame is None or len(frame) < max(2 * window + 1, rsi_period + 1):
        return out

    rsi_series = _rsi_indicator(frame["close"], window=rsi_period).reset_index(drop=True)
    swings = recent_swings(frame, window=window, lookback=lookback, count=10)
    last_index = len(frame) - 1

    lows = [s for s in swings if s.kind == "low"]
    if len(lows) >= 2 and lows[-1].index >= last_index - window:
        out["bullish_divergence"] = float(
            _is_divergence(lows[-2], lows[-1], rsi_series, lower=True,
                            extreme_threshold=extreme_threshold, min_rsi_gap=min_rsi_gap)
        )

    highs = [s for s in swings if s.kind == "high"]
    if len(highs) >= 2 and highs[-1].index >= last_index - window:
        out["bearish_divergence"] = float(
            _is_divergence(highs[-2], highs[-1], rsi_series, lower=False,
                            extreme_threshold=extreme_threshold, min_rsi_gap=min_rsi_gap)
        )

    return out


def _is_divergence(
    prior: SwingPoint,
    latest: SwingPoint,
    rsi_series: pd.Series,
    *,
    lower: bool,
    extreme_threshold: float,
    min_rsi_gap: float,
) -> bool:
    """`latest` extends price in `prior`'s direction while RSI pulls back — that split is the divergence."""
    rsi_prior = rsi_series.iloc[prior.index]
    rsi_latest = rsi_series.iloc[latest.index]
    if rsi_prior != rsi_prior or rsi_latest != rsi_latest:  # NaN — not enough RSI warmup yet
        return False
    # RSI pinned right at its floor/ceiling (an isolated single-day crash/spike,
    # not a sustained move) makes the *next* extreme reading look artificially
    # higher/lower by comparison no matter what price does next — the reading
    # has nowhere left to go but back toward the middle. A small buffer off
    # the absolute boundary keeps the comparison to genuine "this leg is
    # running out of steam" cases rather than that saturation artifact.
    boundary_buffer = 5.0
    if lower:
        return (
            latest.price < prior.price
            and boundary_buffer <= rsi_prior <= extreme_threshold
            and rsi_latest - rsi_prior >= min_rsi_gap
        )
    return (
        latest.price > prior.price
        and 100.0 - extreme_threshold <= rsi_prior <= 100.0 - boundary_buffer
        and rsi_prior - rsi_latest >= min_rsi_gap
    )
