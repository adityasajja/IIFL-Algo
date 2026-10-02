"""Volatility-contraction setups — VCP and Narrow Range.

Both read "is the market coiling right now" off the same idea from opposite
ends: VCP looks at a *sequence* of swings getting tighter over weeks; Narrow
Range looks at a *single* day's range against its own recent history. Neither
promises the breakout that follows actually goes the marked direction — same
posture as `atr.signals.chart_patterns`: a shape flag, not a forecast.
"""

from __future__ import annotations

import pandas as pd

from atr.signals.swing_points import recent_swings


def detect_vcp(
    frame: pd.DataFrame,
    *,
    window: int = 3,
    lookback: int = 150,
    min_contractions: int = 2,
    pivot_tolerance_pct: float = 3.0,
) -> dict[str, float]:
    """Volatility Contraction Pattern (Minervini): a run of pullbacks off a
    shared resistance level, each one shallower than the last, with price
    now back near that resistance — a base "coiling" tighter before a
    breakout attempt.

    Built from the same swing highs/lows as `atr.signals.chart_patterns`,
    read as: take the most recent swing highs that are all within
    `pivot_tolerance_pct`% of the highest one (the shared resistance, or
    "pivot"); between each consecutive pair of those highs, the deepest
    pullback (swing low) must be shallower, as a fraction of the pivot, than
    the pullback before it. `min_contractions` pullbacks satisfying that is
    a VCP; fewer is just "a pullback", not a coiling base.

    Returns ``vcp`` (0.0/1.0), ``vcp_pivot`` (the resistance price, 0.0 if
    no pattern), and ``vcp_contractions`` (how many legs qualified).
    """
    out = {"vcp": 0.0, "vcp_pivot": 0.0, "vcp_contractions": 0.0}
    if frame is None or len(frame) < 2 * window + 1:
        return out

    swings = recent_swings(frame, window=window, lookback=lookback, count=20)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if len(highs) < min_contractions + 1 or not lows:
        return out

    pivot = max(s.price for s in highs)
    if pivot <= 0:
        return out

    # Highs within tolerance of the pivot, oldest first — the shared
    # resistance line the base is contracting under.
    band_highs = sorted(
        (s for s in highs if _close_enough(s.price, pivot, pivot_tolerance_pct)),
        key=lambda s: s.index,
    )
    if len(band_highs) < min_contractions + 1:
        return out

    # The deepest pullback between each consecutive pair of band highs, as a
    # fraction of the pivot — the "how deep did it correct" for that leg.
    depths: list[float] = []
    for a, b in zip(band_highs, band_highs[1:], strict=False):
        between = [s.price for s in lows if a.index < s.index < b.index]
        if not between:
            return out  # no pullback between two highs: not a contracting base
        depths.append((pivot - min(between)) / pivot)

    # Each contraction must be shallower than the one before it.
    contractions = 1
    for prev_depth, depth in zip(depths, depths[1:], strict=False):
        if depth >= prev_depth:
            break
        contractions += 1
    if contractions < min_contractions:
        return out

    last_close = float(frame["close"].iloc[-1])
    if not (last_close <= pivot * (1 + pivot_tolerance_pct / 100.0) and last_close > pivot * (1 - depths[0])):
        return out  # price has to actually be sitting in the base, not somewhere else entirely

    return {"vcp": 1.0, "vcp_pivot": pivot, "vcp_contractions": float(contractions)}


def _close_enough(a: float, b: float, tolerance_pct: float) -> bool:
    base = (abs(a) + abs(b)) / 2.0
    if base == 0:
        return a == b
    return abs(a - b) / base * 100.0 <= tolerance_pct


def narrow_range(frame: pd.DataFrame, n: int) -> pd.Series:
    """True on every bar whose (high - low) range is the smallest of the
    last `n` bars (itself included) — Toby Crabel's NR4/NR7 (``n=4``/``n=7``).

    Range here is the bar's own high-low, not its candle body
    (``|close - open|``): the point of the setup is that the market covered
    unusually little *distance* that day, which the body alone doesn't
    capture — a bar can open-to-close flat while still ranging wide on an
    intraday spike.
    """
    true_range = frame["high"] - frame["low"]
    rolling_min = true_range.rolling(n, min_periods=n).min()
    return (true_range <= rolling_min + 1e-9) & rolling_min.notna()


def narrow_range_context(frame: pd.DataFrame) -> dict[str, float]:
    """``nr4``/``nr7`` — is the *latest* bar a narrow-range-4 / narrow-range-7 day."""
    out = {"nr4": 0.0, "nr7": 0.0}
    if frame is None or frame.empty:
        return out
    if len(frame) >= 4:
        out["nr4"] = float(narrow_range(frame, 4).iloc[-1])
    if len(frame) >= 7:
        out["nr7"] = float(narrow_range(frame, 7).iloc[-1])
    return out
