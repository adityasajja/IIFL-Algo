"""Data hygiene for cached daily bars.

Why this exists
---------------
The IIFL daily cache is not clean. Scanning the Nifty-50 and Midcap-150
universes (~265,000 symbol-bars) turns up 14 bars whose price is 3–5x the level
immediately before *and* after it — and every one of them sits on
**2021-09-15 or 2021-09-16**, across seven different symbols:

===========  =========  ==========  =========
Symbol       Date        Close       Neighbours
===========  =========  ==========  =========
BEL          2021-09-15  207.80      67.47 / 68.43
IRCTC        2021-09-15  3682.25     747.69 / 734.26
JUBLFOOD     2021-09-15  4164.25     823.20 / 821.84
SRF          2021-09-15  11224.60    2135.28 / 2146.14
SCHAEFFLER   2021-09-15  7237.00     1451.53 / 1460.13
===========  =========  ==========  =========

The pattern — a fixed multiplier, one or two sessions, many symbols at once — is
a vendor-side series-mapping error, not a market event. Whatever the cause, the
consequence for this codebase is specific and severe: **a 3x bar on a quiet tape
is a textbook Episodic Pivot.** It is a huge gap on abnormal volume in a stock
that had done nothing for months. Left in, the backtest buys it, and the premise
study reports a +264% twenty-day move that never happened.

Detection
---------
A genuine repricing does not revert. So a bar is dropped when the level
``k`` sessions before and ``k`` sessions after *agree with each other*, while
the bar itself is far from both::

    |close[t-k] / close[t+k] - 1| < 0.35      (the level is stable)
    close[t] / max(close[t-k], close[t+k]) > 1.5   (the bar is not)

The first condition is what separates a glitch from a real move. A stock that
genuinely doubles leaves ``close[t-k]`` and ``close[t+k]`` on opposite sides of
the move, so the guard fails and the bar survives. This matters: a naive
"drop any bar more than 50% from its neighbours" filter would delete exactly the
episodic moves the research is trying to measure.

Rows are dropped, never repaired. Interpolating a price the vendor never printed
would put an invented number into a return series, which is the failure this
module exists to prevent.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

#: Sessions either side used to establish the surrounding level.
DEFAULT_K = 5
#: How closely the level before and after must agree to call the bar transient.
DEFAULT_AGREE = 0.35
#: How far from that level a bar must sit to be considered an excursion.
DEFAULT_FAR = 0.5


def reverting_spike_mask(
    close: pd.Series,
    k: int = DEFAULT_K,
    agree: float = DEFAULT_AGREE,
    far: float = DEFAULT_FAR,
) -> pd.Series:
    """Boolean mask of bars that are far from a level their neighbours share."""
    values = pd.to_numeric(close, errors="coerce").reset_index(drop=True)
    before = values.shift(k)
    after = values.shift(-k)
    both_positive = (before > 0) & (after > 0)

    level_stable = ((before / after) - 1.0).abs() < agree
    excursion = (values / np.maximum(before, after) > 1.0 + far) | (
        values / np.minimum(before, after) < 1.0 / (1.0 + far)
    )
    return (level_stable & excursion & both_positive).fillna(False)


def drop_reverting_spikes(
    frame: pd.DataFrame,
    *,
    close_column: str = "close",
    timestamp_column: str = "ts",
    k: int = DEFAULT_K,
    agree: float = DEFAULT_AGREE,
    far: float = DEFAULT_FAR,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(clean_frame, dropped_rows)``.

    The dropped rows are returned rather than merely counted so a caller can
    print or log what was removed. A silent repair is indistinguishable from a
    silent bug.
    """
    if frame.empty or close_column not in frame.columns:
        return frame, frame.iloc[0:0]

    working = frame
    if timestamp_column in frame.columns:
        working = frame.sort_values(timestamp_column)
    mask = reverting_spike_mask(working[close_column], k=k, agree=agree, far=far)
    if not bool(mask.any()):
        return frame, frame.iloc[0:0]
    return working.loc[~mask].reset_index(drop=True), working.loc[mask]
