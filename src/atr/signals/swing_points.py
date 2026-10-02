"""Swing highs and lows — the shared foundation under chart-pattern detection,
trendlines, and Fibonacci levels.

A "swing high" is a bar whose high is the highest within `window` bars on
either side of it — a local peak; a swing low is the mirror. This is the
standard fractal/pivot definition most charting platforms use, and it is what
every pattern below (head-and-shoulders, triangles, trendlines) is actually
built from: those patterns are *shapes made of swing points*, not something
read directly off the raw candles.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class SwingPoint:
    index: int  # position in the frame, 0-based
    price: float
    kind: str  # "high" or "low"


def find_swings(frame: pd.DataFrame, window: int = 3) -> list[SwingPoint]:
    """Every swing high/low in `frame`, oldest first.

    `window` is how many bars on each side must be lower (for a high) or
    higher (for a low) — 3 is a reasonable default (a 7-bar fractal): tight
    enough to catch normal swings, wide enough that ordinary single-bar noise
    doesn't count as a reversal.
    """
    if frame is None or len(frame) < 2 * window + 1:
        return []

    high = frame["high"].to_numpy()
    low = frame["low"].to_numpy()
    n = len(frame)
    swings: list[SwingPoint] = []
    for i in range(window, n - window):
        lo_slice = high[i - window : i]
        hi_slice = high[i + 1 : i + window + 1]
        if high[i] > lo_slice.max() and high[i] > hi_slice.max():
            swings.append(SwingPoint(index=i, price=float(high[i]), kind="high"))
            continue
        lo_slice2 = low[i - window : i]
        hi_slice2 = low[i + 1 : i + window + 1]
        if low[i] < lo_slice2.min() and low[i] < hi_slice2.min():
            swings.append(SwingPoint(index=i, price=float(low[i]), kind="low"))

    return swings


def recent_swings(frame: pd.DataFrame, window: int = 3, lookback: int = 100, count: int = 6) -> list[SwingPoint]:
    """The last `count` swings within the last `lookback` bars — the window a
    pattern detector actually looks at, since a head-and-shoulders from eight
    months ago says nothing about the current bar.
    """
    if frame is None or frame.empty:
        return []
    recent = frame.tail(min(lookback, len(frame))).reset_index(drop=True)
    offset = len(frame) - len(recent)
    swings = find_swings(recent, window=window)
    swings = [SwingPoint(index=s.index + offset, price=s.price, kind=s.kind) for s in swings]
    return swings[-count:]
