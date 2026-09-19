"""Per-bar "stalled at the top" features. Research and building blocks, NOT an alert rule.

``scripts/study_stall.py`` and ``scripts/study_exit_signals.py`` tested whether "ran up, sits
at its high, has gone flat" precedes weak weeks, over 2,867 stocks and 11 years. It does not:
flagged stocks did slightly *better* than average over the next 5 sessions (+0.65% against
+0.37%), and none of nine topping signals held up in both halves of the history. Stocks at
their highs tend to keep drifting up. So nothing here is used to say "sell, it has peaked".

What survives is narrower: a stock that has gone quiet tends to stay quieter than usual
(``scripts/study_quiet.py``), which ``atr.insights.watch`` reports as a description, not a
forecast of direction. The features are kept because that check and the studies use them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atr.strategy.indicators import rsi

# The score needed on top of the three-part gate.
SCORE_THRESHOLD = 55.0


def stall_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Per-bar features and score. Vectorised so it can be studied historically."""
    df = df.sort_values("ts").reset_index(drop=True)
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    lo = df["low"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float) if "volume" in df.columns else pd.Series(0.0, index=df.index)

    sma50 = c.rolling(50).mean()
    ext50 = (c / sma50 - 1) * 100
    ret20 = (c / c.shift(20) - 1) * 100
    ret5 = (c / c.shift(5) - 1) * 100
    off_high = (c / h.rolling(20).max() - 1) * 100

    span = h - lo
    range_ratio = span.rolling(5).mean() / span.rolling(20).mean().replace(0, np.nan)
    rsi_now = rsi(c)
    rsi_before = rsi_now.shift(5)
    vol_ratio = v.rolling(5).mean() / v.rolling(20).mean().replace(0, np.nan)
    upper_wick = ((h - np.maximum(o, c)) / span.replace(0, np.nan)).rolling(5).mean()

    ran_up = (ret20 >= 10) | (ext50 >= 8)
    at_top = off_high >= -3.5
    stalled = (ret5.abs() <= 2.5) & (range_ratio <= 0.9)

    s_run = ret20.clip(0, 30) / 30 * 30
    s_stall = (2.5 - ret5.abs()).clip(0, 2.5) / 2.5 * 15 + (0.9 - range_ratio).clip(0, 0.5) / 0.5 * 15
    s_momentum = np.where(
        (rsi_before >= 65) & (rsi_before - rsi_now >= 4), 20, np.where(rsi_now >= 65, 8, 0)
    )
    s_distribution = (vol_ratio <= 0.85).astype(float) * 8 + (upper_wick >= 0.35).astype(float) * 12
    score = (s_run.fillna(0) + s_stall.fillna(0) + s_momentum + s_distribution).clip(0, 100)

    return pd.DataFrame(
        {
            "ts": df["ts"],
            "close": c,
            "ret20": ret20,
            "ext50": ext50,
            "ret5": ret5,
            "off_high": off_high,
            "range_ratio": range_ratio,
            "rsi": rsi_now,
            "rsi_before": rsi_before,
            "vol_ratio": vol_ratio,
            "upper_wick": upper_wick,
            "gate": ran_up & at_top & stalled,
            "score": score,
            "flag": ran_up & at_top & stalled & (score >= SCORE_THRESHOLD),
        }
    )

