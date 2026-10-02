"""Candlestick pattern recognition — the one item from the TradingView gap
list that's actually a formula-engine problem: a fixed shape read off the
last one to three bars' open/high/low/close, the same kind of arithmetic
`atr.signals.formula` already does, just applied to *shape* instead of level.

TradingView (and most charting platforms) lean on TA-Lib's C implementation
for its full ~60-pattern catalog; TA-Lib is a genuine pain to install on
Windows (a compiled C library, not a pip package), so this hand-implements the
well-known, commonly-traded patterns directly against pandas — standard
textbook definitions, not TA-Lib's exact numbers, so treat a reading here as
"this pattern's shape is present," not as byte-identical to what TA-Lib would
say for the same bar.

Every pattern reads as 0.0 (absent) or 1.0 (present) on the *latest* bar —
plain named values in the formula context, not callable functions, because
none of these take a period the way `ema(n)` does. Reversal patterns
(hammer, shooting star, engulfing, ...) are shape-only: a hammer's candle
looks the same whether it follows a decline or not, so "is this a bullish
reversal" is `hammer and streak_down >= 2`, combining the shape with the
context that gives it meaning — same as how any other rule in this engine
already composes.
"""

from __future__ import annotations

import pandas as pd


def detect_patterns(frame: pd.DataFrame) -> dict[str, float]:
    """0.0/1.0 flags for every pattern this module recognizes, on the latest bar."""
    if frame is None or len(frame) < 3:
        return {}

    o, h, l, c = frame["open"], frame["high"], frame["low"], frame["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, float("nan"))
    upper_wick = h - pd.concat([o, c], axis=1).max(axis=1)
    lower_wick = pd.concat([o, c], axis=1).min(axis=1) - l
    is_bull = c > o
    is_bear = c < o

    def _v(series: pd.Series) -> float:
        try:
            val = bool(series.iloc[-1])
        except (IndexError, ValueError, TypeError):
            return 0.0
        return 1.0 if val else 0.0

    out: dict[str, float] = {}

    # --- single-bar shapes -----------------------------------------------
    out["doji"] = _v(body <= 0.1 * rng)
    out["marubozu_bull"] = _v(is_bull & (body >= 0.95 * rng))
    out["marubozu_bear"] = _v(is_bear & (body >= 0.95 * rng))
    # Hammer/shooting-star shape: a small body pushed to one end of the
    # range, with a long wick on the other side. Same shape either name —
    # "hammer" at the bottom of a decline, "shooting star" at the top of a
    # rally; both are read here purely by shape (see module docstring).
    hammer_shape = (lower_wick >= 2 * body) & (upper_wick <= 0.3 * body.replace(0, 0.01))
    star_shape = (upper_wick >= 2 * body) & (lower_wick <= 0.3 * body.replace(0, 0.01))
    out["hammer"] = _v(hammer_shape)
    out["shooting_star"] = _v(star_shape)
    out["spinning_top"] = _v((body <= 0.3 * rng) & (upper_wick >= body) & (lower_wick >= body))

    if len(frame) >= 2:
        o1, c1 = o.iloc[-2], c.iloc[-2]
        prev_bull, prev_bear = c1 > o1, c1 < o1
        cur_o, cur_c = o.iloc[-1], c.iloc[-1]
        cur_bull, cur_bear = cur_c > cur_o, cur_c < cur_o
        prev_body = abs(c1 - o1)
        cur_body = abs(cur_c - cur_o)

        out["bullish_engulfing"] = 1.0 if (
            prev_bear and cur_bull and cur_o <= c1 and cur_c >= o1 and cur_body > prev_body
        ) else 0.0
        out["bearish_engulfing"] = 1.0 if (
            prev_bull and cur_bear and cur_o >= c1 and cur_c <= o1 and cur_body > prev_body
        ) else 0.0
        mid1 = (o1 + c1) / 2.0
        out["piercing_line"] = 1.0 if (
            prev_bear and cur_bull and cur_o < l.iloc[-2] and cur_c > mid1 and cur_c < o1
        ) else 0.0
        out["dark_cloud_cover"] = 1.0 if (
            prev_bull and cur_bear and cur_o > h.iloc[-2] and cur_c < mid1 and cur_c > o1
        ) else 0.0
    else:
        out["bullish_engulfing"] = 0.0
        out["bearish_engulfing"] = 0.0
        out["piercing_line"] = 0.0
        out["dark_cloud_cover"] = 0.0

    if len(frame) >= 3:
        o2, c2 = o.iloc[-3], c.iloc[-3]
        o1, c1 = o.iloc[-2], c.iloc[-2]
        cur_o, cur_c = o.iloc[-1], c.iloc[-1]
        body2 = abs(c2 - o2)
        body1 = abs(c1 - o1)

        out["three_white_soldiers"] = 1.0 if (
            c2 > o2 and c1 > o1 and cur_c > cur_o
            and c1 > c2 and cur_c > c1
            and o1 > o2 and cur_o > o1
        ) else 0.0
        out["three_black_crows"] = 1.0 if (
            c2 < o2 and c1 < o1 and cur_c < cur_o
            and c1 < c2 and cur_c < c1
            and o1 < o2 and cur_o < o1
        ) else 0.0
        # Morning/evening star: a decisive first bar, a small-bodied "star"
        # that gaps away from it, then a decisive third bar closing back
        # past the first bar's midpoint.
        mid2 = (o2 + c2) / 2.0
        out["morning_star"] = 1.0 if (
            c2 < o2 and body2 > 0
            and body1 <= 0.5 * body2
            and cur_c > cur_o and cur_c > mid2
        ) else 0.0
        out["evening_star"] = 1.0 if (
            c2 > o2 and body2 > 0
            and body1 <= 0.5 * body2
            and cur_c < cur_o and cur_c < mid2
        ) else 0.0
    else:
        out["three_white_soldiers"] = 0.0
        out["three_black_crows"] = 0.0
        out["morning_star"] = 0.0
        out["evening_star"] = 0.0

    return out


#: name -> meaning, for the same author-facing contract as
#: `atr.signals.formula.INDICATOR_NAMES`. All read 0.0/1.0 — no period, no
#: arguments; reference by plain name, e.g. `hammer == 1 and streak_down >= 2`.
CANDLESTICK_PATTERN_NAMES: dict[str, str] = {
    "doji": "body is at most 10% of the bar's range — open and close nearly equal",
    "marubozu_bull": "a bullish bar with almost no wicks — the body is nearly the whole range",
    "marubozu_bear": "a bearish bar with almost no wicks — the body is nearly the whole range",
    "hammer": "small body pushed to the top with a long lower wick — bullish at the bottom of a decline, read shape-only here",
    "shooting_star": "small body pushed to the bottom with a long upper wick — bearish at the top of a rally, read shape-only here",
    "spinning_top": "small body with wicks on both sides — indecision",
    "bullish_engulfing": "a bullish bar whose body fully engulfs the prior bearish bar's body",
    "bearish_engulfing": "a bearish bar whose body fully engulfs the prior bullish bar's body",
    "piercing_line": "a bullish bar opening below the prior low and closing above the prior bar's midpoint",
    "dark_cloud_cover": "a bearish bar opening above the prior high and closing below the prior bar's midpoint",
    "three_white_soldiers": "three consecutive bullish bars, each closing and opening higher than the last",
    "three_black_crows": "three consecutive bearish bars, each closing and opening lower than the last",
    "morning_star": "a bearish bar, a small-bodied bar, then a bullish bar closing past the first bar's midpoint",
    "evening_star": "a bullish bar, a small-bodied bar, then a bearish bar closing past the first bar's midpoint",
}
