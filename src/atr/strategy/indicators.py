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


def wma(series: pd.Series, window: int) -> pd.Series:
    """Weighted moving average — later bars in the window weighted more heavily.

    Vectorised via a strided view + matrix-multiply rather than
    ``rolling().apply()``, which invokes a Python callback per window and is
    an order of magnitude slower over a large universe.
    """
    values = series.to_numpy(dtype=float)
    weights = np.arange(1, window + 1, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) >= window:
        windows = np.lib.stride_tricks.sliding_window_view(values, window)
        out[window - 1 :] = windows @ weights / weights.sum()
    return pd.Series(out, index=series.index)


def stochastic_k(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Stochastic %K — where the close sits within the window's range, 0-100."""
    lo = low.rolling(window, min_periods=window).min()
    hi = high.rolling(window, min_periods=window).max()
    span = (hi - lo).replace(0, np.nan)
    return ((close - lo) / span * 100.0).fillna(50.0)


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(line, signal, histogram) — the standard 12/26/9 EMA MACD."""
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    d_vals = delta.to_numpy(dtype=float)
    g_vals = np.maximum(d_vals, 0.0)
    l_vals = np.maximum(-d_vals, 0.0)
    alpha = 1.0 / window
    gain = pd.Series(g_vals, index=close.index).ewm(alpha=alpha, adjust=False).mean()
    loss = pd.Series(l_vals, index=close.index).ewm(alpha=alpha, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # Zero average loss with gains present is the strongest possible uptrend:
    # RSI is 100, not undefined. Dividing by the zero made it NaN, and the
    # fillna(50) below then reported a relentlessly rising stock as perfectly
    # neutral — which silently made it qualify as an RSI "pullback".
    out = out.mask((loss <= 0) & (gain > 0), 100.0)
    return out.fillna(50)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    prev = np.empty_like(c)
    prev[0] = np.nan
    prev[1:] = c[:-1]
    hl = h - l
    hp = np.abs(h - prev)
    lp = np.abs(l - prev)
    tr = np.maximum(hl, np.maximum(hp, lp))
    return pd.Series(tr, index=close.index)


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
