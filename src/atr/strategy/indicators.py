"""Small, dependency-free indicator library.

Deliberately plain pandas so they can be used vectorised in
:meth:`Strategy.prepare` (fast) or incrementally on a rolling window in live
trading (same maths, fewer rows).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    return true_range(high, low, close).ewm(alpha=1 / window, adjust=False).mean()


def bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0):
    mid = sma(close, window)
    sd = close.rolling(window, min_periods=window).std(ddof=1)
    return mid, mid + num_std * sd, mid - num_std * sd


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP — resets each calendar day."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].groupby(df.index.date).cumsum()
    cum_pv = (typical * df["volume"]).groupby(df.index.date).cumsum()
    return cum_pv / cum_vol.replace(0, np.nan)


def session_range(df: pd.DataFrame, minutes: int = 15) -> tuple[pd.Series, pd.Series]:
    """Opening-range high/low per trading day, broadcast to every bar.

    Uses the first ``minutes`` bars of each session.
    """
    df = df.copy()
    day = pd.Series(df.index.date, index=df.index)
    minutes_of_day = (df.index.hour * 60 + df.index.minute)
    start_of_day = pd.Series(minutes_of_day, index=df.index).groupby(day).transform("min")
    in_window = minutes_of_day <= start_of_day + minutes
    window = df[in_window]
    high = window.groupby(window.index.date)["high"].max()
    low = window.groupby(window.index.date)["low"].min()
    return day.map(high), day.map(low)


def crossover(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """True on the bar where ``fast`` crosses above ``slow``."""
    return (fast > slow) & (fast.shift(1) <= slow.shift(1))


def crossunder(fast: pd.Series, slow: pd.Series) -> pd.Series:
    return (fast < slow) & (fast.shift(1) >= slow.shift(1))
