"""Chart pattern recognition — built from swing points
(`atr.signals.swing_points`), not the raw candles the way
`atr.signals.candlestick_patterns` reads shape.

These are genuinely a different problem from a candlestick pattern: a
candlestick is a shape in one to three bars; a chart pattern is a shape made
of several *swings*, spread over dozens of bars, matched against geometric
tolerances (are these two shoulders "roughly equal"? is this trendline
"converging"?). Every tolerance below is a judgment call, stated as a comment
where it's made — there is no single textbook number for "roughly equal", and
being explicit about the threshold is more honest than presenting one as
exact.

Every flag here is a plain 0.0/1.0 read on the *latest* bar — "has this
pattern just completed here" — not a callable function, and not a promise
that the pattern will resolve the way its name traditionally suggests: a
head-and-shoulders top is read as a shape, not a guarantee of the breakdown
that shape is said to predict.

Two families, by how they're detected:

* **Swing-based** (everything through the two-trendline classifier): built
  from `find_swings`, the same fractal/pivot highs and lows every pattern
  here and in `atr.signals.trendlines` shares.
* **Curve-based** (cup and handle, rounding top/bottom): a swing-point zigzag
  can't tell a smooth multi-week rounding from a jagged one that happens to
  start and end near the same price, so these instead fit a parabola to the
  raw closes and read its curvature and fit quality.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atr.signals.swing_points import recent_swings


def _close_enough(a: float, b: float, tolerance_pct: float) -> bool:
    """Are `a` and `b` within `tolerance_pct`% of their average? The
    "roughly equal" test most patterns below need at least once (two
    shoulders, two tops, ...) — exact equality never happens in real prices,
    so every pattern definition needs *some* tolerance; 3% is a common
    rule-of-thumb band for this kind of match.
    """
    base = (abs(a) + abs(b)) / 2.0
    if base == 0:
        return a == b
    return abs(a - b) / base * 100.0 <= tolerance_pct


def _fit_line(points: list[tuple[int, float]]) -> tuple[float, float] | None:
    if len(points) < 2:
        return None
    xs = np.array([p[0] for p in points], dtype=float)
    ys = np.array([p[1] for p in points], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    return float(slope), float(intercept)


def detect_chart_patterns(
    frame: pd.DataFrame, *, window: int = 3, lookback: int = 100, tolerance_pct: float = 3.0
) -> dict[str, float]:
    """0.0/1.0 flags for every chart pattern this module recognizes, read as
    "does the most recent swing sequence (or, for the curve-based patterns,
    the most recent price action) form this shape."
    """
    if frame is None or len(frame) < 2 * window + 1:
        return {}

    swings = recent_swings(frame, window=window, lookback=lookback, count=10)

    out = {
        "head_and_shoulders": 0.0,
        "inverse_head_and_shoulders": 0.0,
        "double_top": 0.0,
        "double_bottom": 0.0,
        "triple_top": 0.0,
        "triple_bottom": 0.0,
        "ascending_triangle": 0.0,
        "descending_triangle": 0.0,
        "symmetric_triangle": 0.0,
        "rising_wedge": 0.0,
        "falling_wedge": 0.0,
        "rectangle": 0.0,
        "broadening_formation": 0.0,
        "bull_flag": 0.0,
        "bear_flag": 0.0,
        "bullish_pennant": 0.0,
        "bearish_pennant": 0.0,
        "cup_and_handle": 0.0,
        "rounding_bottom": 0.0,
        "rounding_top": 0.0,
        # Confirmed breakout / breakdown variants (clearing neckline / resistance / support on volume)
        "double_bottom_breakout": 0.0,
        "double_top_breakdown": 0.0,
        "inverse_hs_breakout": 0.0,
        "head_shoulders_breakdown": 0.0,
        "ascending_triangle_breakout": 0.0,
        "rectangle_breakout": 0.0,
    }

    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]

    # Current bar close and volume for breakout evaluation
    curr_close = float(frame["close"].iloc[-1])
    vol_col = frame["volume"] if "volume" in frame.columns else None
    vol_ratio = 1.0
    if vol_col is not None and len(vol_col) >= 20:
        avg_vol = float(vol_col.tail(20).mean())
        if avg_vol > 0:
            vol_ratio = float(vol_col.iloc[-1]) / avg_vol
    vol_confirmed = vol_ratio >= 1.2 or vol_col is None

    # --- head and shoulders: peak, higher peak, peak, roughly matched shoulders ---
    # The three most recent peaks *in order* — `highs` already preserves order,
    # so `highs[-3:]` is left shoulder, head, right shoulder.
    if len(highs) >= 3:
        l, h, r = highs[-3], highs[-2], highs[-1]
        if h.price > l.price and h.price > r.price and _close_enough(l.price, r.price, tolerance_pct):
            out["head_and_shoulders"] = 1.0
            # Neckline is the intervening swing lows between shoulders
            inter_lows = [s.price for s in lows if l.index < s.index < r.index]
            neckline = min(inter_lows) if inter_lows else None
            if neckline is not None and curr_close < neckline and vol_confirmed:
                out["head_shoulders_breakdown"] = 1.0
    if len(lows) >= 3:
        l, h, r = lows[-3], lows[-2], lows[-1]
        if h.price < l.price and h.price < r.price and _close_enough(l.price, r.price, tolerance_pct):
            out["inverse_head_and_shoulders"] = 1.0
            inter_highs = [s.price for s in highs if l.index < s.index < r.index]
            neckline = max(inter_highs) if inter_highs else None
            if neckline is not None and curr_close > neckline and vol_confirmed:
                out["inverse_hs_breakout"] = 1.0

    # --- double / triple top / bottom -------------------------------------
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        between_low = min((s.price for s in lows if a.index < s.index < b.index), default=None)
        if _close_enough(a.price, b.price, tolerance_pct) and between_low is not None and between_low < min(a.price, b.price):
            out["double_top"] = 1.0
            if curr_close < between_low and vol_confirmed:
                out["double_top_breakdown"] = 1.0
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        between_high = max((s.price for s in highs if a.index < s.index < b.index), default=None)
        if _close_enough(a.price, b.price, tolerance_pct) and between_high is not None and between_high > max(a.price, b.price):
            out["double_bottom"] = 1.0
            if curr_close > between_high and vol_confirmed:
                out["double_bottom_breakout"] = 1.0
    if len(highs) >= 3:
        a, b, c = highs[-3], highs[-2], highs[-1]
        if (
            _close_enough(a.price, b.price, tolerance_pct)
            and _close_enough(b.price, c.price, tolerance_pct)
            and _close_enough(a.price, c.price, tolerance_pct)
        ):
            out["triple_top"] = 1.0
    if len(lows) >= 3:
        a, b, c = lows[-3], lows[-2], lows[-1]
        if (
            _close_enough(a.price, b.price, tolerance_pct)
            and _close_enough(b.price, c.price, tolerance_pct)
            and _close_enough(a.price, c.price, tolerance_pct)
        ):
            out["triple_bottom"] = 1.0

    # --- the two-trendline family: triangles, wedges, rectangle, broadening ---
    # Every one of these is "fit a line through the recent swing highs, fit
    # another through the recent swing lows, then read the two slopes and
    # whether the lines are converging, parallel, or diverging":
    #
    #               low slope ≈ 0        low slope > 0        low slope < 0
    #  high ≈ 0      rectangle        ascending triangle      (falling, not
    #                (flat channel)   (flat top, rising                    )
    #                                  floor)
    #  high > 0      (rising, not     rising wedge             broadening
    #                        )        (both rise, converging)  (diverges)
    #  high < 0      descending       symmetric triangle       (n/a — both
    #                triangle         (converge, opposite       falling and
    #                (flat floor,     signs)                    diverging is
    #                 falling top)                               just a
    #                                                             down move)
    #
    # "Converging" / "diverging" is read from the fitted lines' actual gap at
    # the oldest vs. the newest swing, not just the two slopes' signs, because
    # a wedge and a channel can have the same-signed slopes and only differ in
    # whether the gap between them is shrinking.
    if len(highs) >= 2 and len(lows) >= 2:
        high_fit = _fit_line([(s.index, s.price) for s in highs])
        low_fit = _fit_line([(s.index, s.price) for s in lows])
        if high_fit and low_fit:
            start_idx = min(highs[0].index, lows[0].index)
            end_idx = max(highs[-1].index, lows[-1].index)
            gap_start = (high_fit[0] * start_idx + high_fit[1]) - (low_fit[0] * start_idx + low_fit[1])
            gap_end = (high_fit[0] * end_idx + high_fit[1]) - (low_fit[0] * end_idx + low_fit[1])
            avg_price = float(frame["close"].tail(lookback).mean())
            # A slope under ~0.02% of the average price per bar reads as flat —
            # real swing highs are never perfectly level, so "flat" has to mean
            # "close enough that a person would call it a horizontal line."
            flat_threshold = 0.0002 * avg_price
            high_flat = abs(high_fit[0]) < flat_threshold
            low_flat = abs(low_fit[0]) < flat_threshold
            converging = gap_end < gap_start * 0.85  # the gap shrank by at least 15%
            diverging = gap_end > gap_start * 1.15
            resistance_level = max(s.price for s in highs)

            if high_flat and low_flat:
                out["rectangle"] = 1.0
                if curr_close > resistance_level and vol_confirmed:
                    out["rectangle_breakout"] = 1.0
            elif high_flat and low_fit[0] > 0:
                out["ascending_triangle"] = 1.0
                if curr_close > resistance_level and vol_confirmed:
                    out["ascending_triangle_breakout"] = 1.0
            elif low_flat and high_fit[0] < 0:
                out["descending_triangle"] = 1.0
            elif high_fit[0] < 0 and low_fit[0] > 0 and converging:
                out["symmetric_triangle"] = 1.0
            elif high_fit[0] > 0 and low_fit[0] < 0 and diverging:
                out["broadening_formation"] = 1.0
            elif high_fit[0] > 0 and low_fit[0] > 0 and converging:
                out["rising_wedge"] = 1.0
            elif high_fit[0] < 0 and low_fit[0] < 0 and converging:
                out["falling_wedge"] = 1.0

    out.update(_detect_flags_and_pennants(frame))
    out.update(_detect_curve_patterns(frame))
    return out


def _detect_flags_and_pennants(
    frame: pd.DataFrame, *, pole_bars: int = 10, consolidation_bars: int = 8,
    pole_threshold_pct: float = 6.0, tight_range_pct: float = 4.0,
) -> dict[str, float]:
    """A flag or pennant: a sharp directional move (the "pole"), immediately
    followed by a tight consolidation. A flag's consolidation drifts gently
    against the pole in a roughly parallel channel; a pennant's converges
    instead, like a small triangle. Distinguishing the two only matters for
    the name — both read the same way to a trader (continuation, same
    direction as the pole).
    """
    out = {"bull_flag": 0.0, "bear_flag": 0.0, "bullish_pennant": 0.0, "bearish_pennant": 0.0}
    total = pole_bars + consolidation_bars
    if len(frame) < total + 1:
        return out

    close = frame["close"]
    pole_start = close.iloc[-total - 1]
    pole_end = close.iloc[-consolidation_bars - 1]
    if pole_start <= 0:
        return out
    pole_return_pct = (pole_end / pole_start - 1.0) * 100.0

    consolidation = frame.iloc[-consolidation_bars:]
    cons_high, cons_low = float(consolidation["high"].max()), float(consolidation["low"].min())
    cons_range_pct = (cons_high - cons_low) / pole_end * 100.0 if pole_end else 100.0
    if cons_range_pct > tight_range_pct:
        return out  # not tight enough to be a consolidation at all

    half = max(consolidation_bars // 2, 2)
    early_range = float(consolidation["high"].iloc[:half].max() - consolidation["low"].iloc[:half].min())
    late_range = float(consolidation["high"].iloc[-half:].max() - consolidation["low"].iloc[-half:].min())
    narrowing = late_range < early_range * 0.7

    cons_slope = float(np.polyfit(range(len(consolidation)), consolidation["close"].to_numpy(), 1)[0])

    if pole_return_pct >= pole_threshold_pct:
        if narrowing:
            out["bullish_pennant"] = 1.0
        elif cons_slope <= 0:
            out["bull_flag"] = 1.0
    elif pole_return_pct <= -pole_threshold_pct:
        if narrowing:
            out["bearish_pennant"] = 1.0
        elif cons_slope >= 0:
            out["bear_flag"] = 1.0

    return out


def _detect_curve_patterns(
    frame: pd.DataFrame, *, window_bars: int = 45, handle_bars: int = 8, min_fit: float = 0.6,
) -> dict[str, float]:
    """Cup-and-handle and rounding top/bottom — read from curvature, not
    swings. Fits a parabola to the last `window_bars` closes: a good fit
    (`min_fit`, an R² threshold — 0.6 is a loose "clearly curved, not noise"
    bar, not a strict one) with positive curvature is a rounding bottom
    (a cup); negative curvature is a rounding top. A cup additionally needs
    a shallow pullback (the handle) in the final `handle_bars`, after the
    right side has recovered most of the way back to the rim.
    """
    out = {"cup_and_handle": 0.0, "rounding_bottom": 0.0, "rounding_top": 0.0}
    if frame is None or len(frame) < window_bars + 1:
        return out

    window = frame["close"].tail(window_bars).to_numpy()
    xs = np.arange(len(window), dtype=float)
    coeffs = np.polyfit(xs, window, 2)
    fitted = np.polyval(coeffs, xs)
    ss_res = float(np.sum((window - fitted) ** 2))
    ss_tot = float(np.sum((window - window.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    if r2 < min_fit:
        return out

    a = coeffs[0]  # curvature: positive = U (cup), negative = inverted-U (top)
    vertex_x = -coeffs[1] / (2 * a) if a != 0 else -1.0
    # The low/high point of the curve should sit somewhere in the middle
    # third of the window — a parabola whose vertex is right at one edge is
    # really just a trend, not a rounding shape.
    vertex_in_middle = window_bars * 0.25 <= vertex_x <= window_bars * 0.75
    # Rims (the two ends) should be roughly level with each other — a
    # rounding shape returns close to where it started, unlike a one-sided
    # curve that's really just a decelerating trend.
    rims_level = _close_enough(float(window[0]), float(window[-1]), 8.0)

    if a > 0 and vertex_in_middle and rims_level:
        out["rounding_bottom"] = 1.0
        if len(frame) >= handle_bars:
            handle = frame["close"].tail(handle_bars).to_numpy()
            rim = float(window.max())
            handle_low = float(handle.min())
            pullback_pct = (rim - handle_low) / rim * 100.0 if rim else 0.0
            near_rim = float(handle[-1]) >= rim * 0.95
            # A handle is a shallow pullback (well short of round-tripping
            # back to the cup's own low) that's since recovered toward the rim.
            if 1.0 <= pullback_pct <= 15.0 and near_rim:
                out["cup_and_handle"] = 1.0
    elif a < 0 and vertex_in_middle and rims_level:
        out["rounding_top"] = 1.0

    return out


#: name -> meaning, same author-facing contract as the other indicator modules.
CHART_PATTERN_NAMES: dict[str, str] = {
    "head_and_shoulders": "three peaks, the middle one highest, the outer two roughly equal — classic top reversal shape",
    "inverse_head_and_shoulders": "three troughs, the middle one lowest, the outer two roughly equal — classic bottom reversal shape",
    "double_top": "two roughly-equal peaks with a dip between them",
    "double_bottom": "two roughly-equal troughs with a rally between them",
    "triple_top": "three roughly-equal peaks",
    "triple_bottom": "three roughly-equal troughs",
    "ascending_triangle": "a flat resistance line with rising swing lows — buyers stepping in progressively higher",
    "descending_triangle": "a flat support line with falling swing highs — sellers stepping in progressively lower",
    "symmetric_triangle": "converging swing highs and lows — range compressing toward a breakout either way",
    "rising_wedge": "both swing highs and lows rising, but converging — often a bearish reversal despite the upward slope",
    "falling_wedge": "both swing highs and lows falling, but converging — often a bullish reversal despite the downward slope",
    "rectangle": "roughly flat swing highs and lows — a sideways range/channel",
    "broadening_formation": "swing highs rising and swing lows falling at once — the range is widening (a 'megaphone')",
    "bull_flag": "a sharp rally, then a tight, gently-drifting-down consolidation — continuation setup",
    "bear_flag": "a sharp decline, then a tight, gently-drifting-up consolidation — continuation setup",
    "bullish_pennant": "a sharp rally, then a tight, narrowing consolidation — continuation setup",
    "bearish_pennant": "a sharp decline, then a tight, narrowing consolidation — continuation setup",
    "cup_and_handle": "a rounded U-shaped recovery back near its starting level, followed by a shallow pullback",
    "rounding_bottom": "a smooth, gradual U-shaped reversal from a decline back up near the starting level",
    "rounding_top": "a smooth, gradual inverted-U reversal from a rally back down near the starting level",
    "double_bottom_breakout": "double bottom pattern confirmed by price breaking above intervening neckline on volume",
    "double_top_breakdown": "double top pattern confirmed by price breaking below intervening neckline on volume",
    "inverse_hs_breakout": "inverse head-and-shoulders pattern confirmed by price breaking above neckline on volume",
    "head_shoulders_breakdown": "head-and-shoulders pattern confirmed by price breaking below neckline on volume",
    "ascending_triangle_breakout": "ascending triangle pattern confirmed by price breaking above flat resistance on volume",
    "rectangle_breakout": "rectangle / sideways range confirmed by price breaking above upper resistance on volume",
}
