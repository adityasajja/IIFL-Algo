"""Gann Square of 9 — support/resistance levels projected from a base price
via the square-root spiral, anchored on the previous session's close.

Unlike Fibonacci retracements or trendlines, this isn't something a person
draws by eye — it's a fixed numerological construction with one well-known
formula, which is exactly why it fits the same safe, computable pattern as
every other indicator here rather than needing a "drawing tool" workaround.
It's included specifically because it's a routine part of Indian intraday
trading culture (a standard feature on Indian retail charting platforms and
a staple of NSE/BSE day-trading calculators) in a way it isn't as much
elsewhere.

The construction: take the square root of the base price, then step outward
in increments of 0.25 (each step corresponds to a 45° turn around the
spiral — the "cardinal" and "ordinal" cross points Gann's own notes describe),
squaring back to get a price level at each step. Eight steps up (resistance)
and eight steps down (support) is the conventional range — going further out
than that offers little practical signal for the noise it adds. Verified
against the standard reference numbers published for a base of 100 (₹100 →
R1 105.06, R2 110.25, R3 115.56, R4 121.00, ...), the numbers most Gann
Square of 9 calculators cite as their own worked example.
"""

from __future__ import annotations

import math

import pandas as pd

#: 8 steps out in each direction — the conventional range; see module docstring.
_STEPS = 8
_INCREMENT = 0.25


def gann_square_of_9(base_price: float) -> dict[str, float]:
    """8 resistance levels and 8 support levels from `base_price`."""
    out: dict[str, float] = {}
    if base_price is None or base_price <= 0 or base_price != base_price:  # NaN check
        for i in range(1, _STEPS + 1):
            out[f"gann_r{i}"] = 0.0
            out[f"gann_s{i}"] = 0.0
        return out

    sqrt_base = math.sqrt(base_price)
    for i in range(1, _STEPS + 1):
        increment = i * _INCREMENT
        out[f"gann_r{i}"] = round((sqrt_base + increment) ** 2, 4)
        support_root = sqrt_base - increment
        out[f"gann_s{i}"] = round(support_root ** 2, 4) if support_root > 0 else 0.0
    return out


def gann_context(frame: pd.DataFrame) -> dict[str, float]:
    """Gann Square of 9 levels anchored on the previous completed session's
    close — the same "previous session" derivation `atr.signals.pivot_points`
    uses, so the two sit on the same footing when a strategy uses both.
    """
    from atr.signals.pivot_points import _previous_session_hlc

    hlc = _previous_session_hlc(frame)
    if hlc is None:
        return gann_square_of_9(0.0)
    _, _, prev_close = hlc
    return gann_square_of_9(prev_close)


#: name -> meaning, same author-facing contract as the other indicator modules.
GANN_NAMES: dict[str, str] = {
    **{f"gann_r{i}": f"Gann Square of 9 resistance {i} ({i * 45}° around the spiral from the previous close)" for i in range(1, _STEPS + 1)},
    **{f"gann_s{i}": f"Gann Square of 9 support {i} ({i * 45}° around the spiral from the previous close)" for i in range(1, _STEPS + 1)},
}


# ─── Gann Angles ────────────────────────────────────────────────────────────
# A different Gann tool from the Square of 9 above: a trend line projected
# from a significant swing point where price advances a fixed amount per bar
# — the famous "1x1" (one price unit per one time unit) being a 45° angle,
# with steeper (2x1, 4x1) and shallower (1x2, 1x4) variants.
#
# Where this needs an honest caveat the Square of 9 didn't: Gann's original
# "one point per day" assumed the price levels commodities traded at in his
# own era, and there is no single universally-agreed way to rescale that for
# a modern price level — ask five Gann practitioners how to scale the angle
# for a ₹2,000 stock and several different answers come back. This uses ATR
# as the per-bar unit instead of a fixed point value: it ties "how much
# should price move per bar to still count as trending along the angle" to
# the stock's own actual volatility, which is a defensible modern adaptation,
# not Gann's own original convention. Treat these as "a slope calibrated to
# this stock's volatility," not as the one true Gann angle.
def gann_angle_context(frame: pd.DataFrame, *, window: int = 3, lookback: int = 100) -> dict[str, float]:
    """1x1/2x1/4x1 up-angles from the most recent swing low, and 1x1/2x1/4x1
    down-angles from the most recent swing high, projected to the latest bar.
    """
    from atr.strategy.indicators import atr as _atr
    from atr.signals.swing_points import recent_swings

    names = [
        "gann_angle_1x1_up", "gann_angle_2x1_up", "gann_angle_4x1_up",
        "gann_angle_1x1_down", "gann_angle_2x1_down", "gann_angle_4x1_down",
    ]
    zero = dict.fromkeys(names, 0.0)
    if frame is None or len(frame) < 2 * window + 15:
        return zero

    atr_val = float(_atr(frame["high"], frame["low"], frame["close"], 14).iloc[-1])
    if not (atr_val > 0):
        return zero

    swings = recent_swings(frame, window=window, lookback=lookback, count=10)
    last_index = len(frame) - 1
    out = dict(zero)

    lows = [s for s in swings if s.kind == "low"]
    if lows:
        low = lows[-1]
        bars = max(last_index - low.index, 0)
        out["gann_angle_1x1_up"] = round(low.price + 1.0 * atr_val * bars, 4)
        out["gann_angle_2x1_up"] = round(low.price + 2.0 * atr_val * bars, 4)
        out["gann_angle_4x1_up"] = round(low.price + 4.0 * atr_val * bars, 4)

    highs = [s for s in swings if s.kind == "high"]
    if highs:
        high = highs[-1]
        bars = max(last_index - high.index, 0)
        out["gann_angle_1x1_down"] = round(high.price - 1.0 * atr_val * bars, 4)
        out["gann_angle_2x1_down"] = round(high.price - 2.0 * atr_val * bars, 4)
        out["gann_angle_4x1_down"] = round(high.price - 4.0 * atr_val * bars, 4)

    return out


GANN_ANGLE_NAMES: dict[str, str] = {
    "gann_angle_1x1_up": "the 1x1 (45°) up-angle from the most recent swing low — ATR-scaled, see module docstring",
    "gann_angle_2x1_up": "the 2x1 (steeper) up-angle from the most recent swing low",
    "gann_angle_4x1_up": "the 4x1 (steepest) up-angle from the most recent swing low",
    "gann_angle_1x1_down": "the 1x1 (45°) down-angle from the most recent swing high",
    "gann_angle_2x1_down": "the 2x1 (steeper) down-angle from the most recent swing high",
    "gann_angle_4x1_down": "the 4x1 (steepest) down-angle from the most recent swing high",
}
