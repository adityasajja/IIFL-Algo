"""Support/resistance trendlines and Fibonacci retracement levels — the
"drawing tool" half of the TradingView gap, made computable: both are just
arithmetic once you have swing points (`atr.signals.swing_points`) or a
swing high/low pair, which is exactly what a person eyeballs when they draw
either by hand.

A trendline through swing points is fit with a least-squares line, then
projected forward to the latest bar to read "where is the line right now" —
the number a formula actually needs, not the line itself. Fibonacci levels
are the standard fixed ratios (23.6/38.2/50/61.8/78.6%) applied to the most
recent significant swing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atr.signals.swing_points import recent_swings

#: The standard Fibonacci retracement ratios. 50% isn't a Fibonacci ratio at
#: all, but every platform includes it anyway — it's there by convention.
_FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)


def _fit_line(points: list[tuple[int, float]]) -> tuple[float, float] | None:
    """(slope, intercept) of a least-squares line through (index, price)
    points, or None with fewer than two."""
    if len(points) < 2:
        return None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    return float(slope), float(intercept)


def trendline_context(frame: pd.DataFrame, *, window: int = 3, lookback: int = 100) -> dict[str, float]:
    """Where the current resistance/support trendlines sit, projected to the
    latest bar, plus whether price has just broken through either.

    Uses every swing high in the lookback window for resistance, every swing
    low for support — not just the two most recent — so the line reflects the
    whole visible trend, the same way a person draws one through *several*
    touches, not just the last two.
    """
    zero = {
        "resistance_trendline": 0.0, "support_trendline": 0.0,
        "trendline_breakout_up": 0.0, "trendline_breakout_down": 0.0,
    }
    if frame is None or len(frame) < 2 * window + 1:
        return zero

    swings = recent_swings(frame, window=window, lookback=lookback, count=20)
    highs = [(s.index, s.price) for s in swings if s.kind == "high"]
    lows = [(s.index, s.price) for s in swings if s.kind == "low"]
    last_index = len(frame) - 1
    last_close = float(frame["close"].iloc[-1])
    # The bar just before the latest one — a breakout is measured from where
    # price *was*, so a single already-past-the-line bar doesn't repeatedly
    # re-report "just broke out" on every later bar too.
    prev_close = float(frame["close"].iloc[-2]) if len(frame) > 1 else last_close

    resistance_fit = _fit_line(highs)
    support_fit = _fit_line(lows)

    resistance_now = resistance_fit[0] * last_index + resistance_fit[1] if resistance_fit else 0.0
    support_now = support_fit[0] * last_index + support_fit[1] if support_fit else 0.0
    resistance_prev_bar = resistance_fit[0] * (last_index - 1) + resistance_fit[1] if resistance_fit else 0.0
    support_prev_bar = support_fit[0] * (last_index - 1) + support_fit[1] if support_fit else 0.0

    breakout_up = bool(resistance_fit and prev_close <= resistance_prev_bar and last_close > resistance_now)
    breakout_down = bool(support_fit and prev_close >= support_prev_bar and last_close < support_now)

    return {
        "resistance_trendline": resistance_now,
        "support_trendline": support_now,
        "trendline_breakout_up": 1.0 if breakout_up else 0.0,
        "trendline_breakout_down": 1.0 if breakout_down else 0.0,
    }


def fibonacci_context(frame: pd.DataFrame, *, window: int = 3, lookback: int = 100) -> dict[str, float]:
    """Fibonacci retracement levels for the most recent significant swing
    (the last confirmed swing high and the last confirmed swing low in the
    lookback window, whichever came more recently defining the *direction* —
    a retracement is measured from the most recent extreme back toward the
    one before it).
    """
    names = {f"fib_{int(r * 1000)}": 0.0 for r in _FIB_RATIOS}
    names["fib_0"] = 0.0
    names["fib_100"] = 0.0
    if frame is None or len(frame) < 2 * window + 1:
        return names

    swings = recent_swings(frame, window=window, lookback=lookback, count=2)
    if len(swings) < 2:
        return names

    a, b = swings[-2], swings[-1]  # a is older, b is the most recent swing
    hi, lo = (a.price, b.price) if a.price > b.price else (b.price, a.price)
    span = hi - lo
    if span <= 0:
        return names

    # If the most recent swing is the low, the move was down and retracement
    # levels count *up* from the low (0% at the low, 100% at the high) — and
    # the mirror when the most recent swing is the high.
    rising_from_low = b.kind == "low"
    out: dict[str, float] = {}
    for r in _FIB_RATIOS:
        level = lo + span * r if rising_from_low else hi - span * r
        out[f"fib_{int(r * 1000)}"] = round(level, 4)
    out["fib_0"] = lo if rising_from_low else hi
    out["fib_100"] = hi if rising_from_low else lo
    return out


#: name -> meaning, same author-facing contract as the other indicator modules.
TRENDLINE_NAMES: dict[str, str] = {
    "resistance_trendline": "current projected level of the resistance line fit through recent swing highs (0 if none)",
    "support_trendline": "current projected level of the support line fit through recent swing lows (0 if none)",
    "trendline_breakout_up": "price just closed above the resistance trendline, having been at/below it the bar before",
    "trendline_breakout_down": "price just closed below the support trendline, having been at/above it the bar before",
    "fib_236": "23.6% Fibonacci retracement level of the most recent swing",
    "fib_382": "38.2% Fibonacci retracement level of the most recent swing",
    "fib_500": "50% retracement level of the most recent swing (not a true Fibonacci ratio, included by convention)",
    "fib_618": "61.8% Fibonacci retracement level of the most recent swing — the most commonly watched one",
    "fib_786": "78.6% Fibonacci retracement level of the most recent swing",
    "fib_0": "the 0% level — the swing's own starting extreme",
    "fib_100": "the 100% level — the swing's own ending extreme",
}
