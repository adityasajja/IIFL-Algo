"""Capital-rotation signal: a held winner that has stopped moving, versus a
symbol that is moving right now.

The exit rules in ``atr.alerts.intelligent`` all fire on damage — a trend
breakdown, an overbought reversal, a trailing stop, a stop-loss. This one
fires on *opportunity cost*: nothing is wrong with the stalled position, it
just isn't doing anything, and the same capital sitting in a stock trending
right now would be.

"Stalled" is read relative to the stock's own recent volatility rather than a
fixed percentage band: a ₹3 range means something different for a ₹50 stock
than a ₹3,000 one, and a stock that normally swings 4% a day standing still
for a week is a much stronger statement than the same stillness in a stock
that never moves much anyway. ATR(14) is that per-stock baseline.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from atr.strategy.indicators import rsi, sma


def _atr14(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / 14, adjust=False).mean()


@dataclass
class StallReading:
    days_stalled: int
    #: The window's high-low range as a multiple of ATR(14). Below the
    #: configured threshold means the stock moved less over the whole window
    #: than its own ATR says a *single day* normally accounts for.
    range_over_atr: float
    gain_from_avg_pct: float
    high_in_window: float
    last_price: float


def read_stall(
    df: pd.DataFrame,
    avg_price: float,
    *,
    window_days: int,
    tight_atr_mult: float,
    min_gain_pct: float,
) -> StallReading | None:
    """Is this position a winner that has gone quiet? None if it isn't, or
    there isn't enough history to say.

    Two gates, both required:

    1. Currently up at least `min_gain_pct` from the average buy price — this
       is about a winner going quiet, not a loser going nowhere, which the
       existing stop-loss rule already covers on its own terms.
    2. The high-low range over the last `window_days` sessions is tight
       relative to this stock's own ATR(14) — whatever was driving the
       earlier move has faded, measured against how much this stock normally
       moves rather than an arbitrary percentage.
    """
    if len(df) < window_days + 20 or avg_price <= 0:
        return None

    close = df["close"]
    last = float(close.iloc[-1])
    gain_pct = (last / avg_price - 1) * 100.0
    if gain_pct < min_gain_pct:
        return None

    atr = _atr14(df)
    last_atr = float(atr.iloc[-1])
    if not (last_atr > 0):
        return None

    window = df.iloc[-window_days:]
    hi, lo = float(window["high"].max()), float(window["low"].min())
    range_over_atr = round((hi - lo) / last_atr, 2)
    if range_over_atr > tight_atr_mult:
        return None

    return StallReading(
        days_stalled=window_days,
        range_over_atr=range_over_atr,
        gain_from_avg_pct=round(gain_pct, 1),
        high_in_window=hi,
        last_price=last,
    )


@dataclass
class MomentumCandidate:
    symbol: str
    price: float
    roc_pct: float
    rsi: float


def best_momentum_candidate(
    frames: dict[str, pd.DataFrame],
    exclude: set[str],
    *,
    lookback_days: int = 20,
) -> MomentumCandidate | None:
    """The strongest *established, healthy* uptrend in `frames`, excluding `exclude`.

    "Healthy" rules out the exact kind of move the RSI-overbought exit rule
    would flag on day one: RSI in 55-70 (trending, not yet stretched), price
    above both SMA20 and SMA50 (an established trend, not a one-day spike).
    Ranked by `lookback_days` rate of change, so among qualifying candidates
    the one actually moving the most wins — this is a candidate list of one,
    not a ranked scan, because a rotation signal names exactly one place to
    put the capital.
    """
    best: MomentumCandidate | None = None
    min_bars = lookback_days + 55
    for symbol, df in frames.items():
        if symbol in exclude or df is None or len(df) < min_bars:
            continue
        close = df["close"]
        last = float(close.iloc[-1])
        prior = float(close.iloc[-lookback_days - 1])
        if prior <= 0 or last <= 0:
            continue
        roc = (last / prior - 1) * 100.0
        last_rsi = float(rsi(close).iloc[-1])
        s20 = float(sma(close, 20).iloc[-1])
        s50 = float(sma(close, 50).iloc[-1])
        if not (55.0 <= last_rsi <= 70.0 and last > s20 > s50):
            continue
        if best is None or roc > best.roc_pct:
            best = MomentumCandidate(symbol=symbol, price=last, roc_pct=round(roc, 1), rsi=round(last_rsi, 1))
    return best
