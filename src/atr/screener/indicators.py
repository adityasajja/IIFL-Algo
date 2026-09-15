"""The indicator catalog: every value a screener condition may test.

A condition names an indicator by key, optionally with a period. This module
resolves that name to a ``pandas.Series`` aligned to the frame's rows.

Design rules
------------

**One source of maths.** Every series delegates to ``atr.strategy.indicators``.
Nothing is computed twice. If the screener's RSI and the backtest's RSI ever
disagreed, a screen would select symbols a strategy could not trade — which is
the same class of failure as ``eval_entry`` drifting from the scanner, and the
shared rule layer exists precisely to prevent it.

**Missing is missing.** A series that cannot be computed — not enough history, a
division by zero, an unknown symbol — returns an all-NaN series rather than
raising or substituting a zero. A condition comparing against NaN is false, so an
unmeasurable symbol simply does not match. Returning ``0`` would instead make it
match every ``<`` screen, which is how a screener fills up with penny stocks.

**Availability is declared.** ``IndicatorSpec.available`` is ``False`` for
anything this build cannot honestly compute (market cap, P/E, open interest).
The UI shows those greyed out with the reason, and a condition on one is rejected
rather than silently evaluating to false — "no data" and "did not match" are
different answers and must not be reported as the same thing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from atr.strategy.indicators import atr as _atr
from atr.strategy.indicators import ema, rsi, sma

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _close(df: pd.DataFrame) -> pd.Series:
    return df["close"].astype(float)


def _na_like(df: pd.DataFrame) -> pd.Series:
    """An all-NaN series on the frame's index: the honest "cannot compute"."""
    return pd.Series(np.nan, index=df.index, dtype=float)


def _safe(fn: Callable[[], pd.Series], index: pd.Index) -> pd.Series:
    """Run a series computation, degrading to NaN on any failure."""
    try:
        series = fn()
    except Exception:  # noqa: BLE001 - an uncomputable series is not an error
        return pd.Series(np.nan, index=index, dtype=float)
    if series is None:
        return pd.Series(np.nan, index=index, dtype=float)
    series = pd.Series(series, index=index, dtype=float)
    return series.replace([np.inf, -np.inf], np.nan)


def _shifted(df: pd.DataFrame, column: str, bars: int) -> pd.Series:
    """The value of *column* ``bars`` sessions ago, aligned to the frame."""
    return df[column].astype(float).shift(bars)


def _rolling_extreme(df: pd.DataFrame, column: str, window: int, fn: str) -> pd.Series:
    return getattr(df[column].astype(float).rolling(window, min_periods=1), fn)()


# ---------------------------------------------------------------------------
# the spec
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IndicatorSpec:
    """One testable value.

    ``fn`` builds the series for a whole frame; the condition layer then reads
    the last row. Computing the series once over the frame rather than per row is
    the difference between a screen that returns and one that hangs.
    """

    key: str
    label: str
    group: str
    unit: str
    #: Whether the condition's period argument is meaningful for this indicator.
    takes_period: bool = False
    #: The period used when a condition omits one.
    default_period: int | None = None
    #: False when this build cannot compute it. See the module docstring.
    available: bool = True
    requires: str = ""
    description: str = ""
    fn: Callable[[pd.DataFrame, int | None], pd.Series] | None = None

    def series(self, df: pd.DataFrame, period: int | None = None) -> pd.Series:
        if self.fn is None or not self.available:
            return _na_like(df)
        return _safe(lambda: self.fn(df, period), df.index)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "unit": self.unit,
            "takes_period": self.takes_period,
            "default_period": self.default_period,
            "available": self.available,
            "requires": self.requires,
            "description": self.description,
        }


def _ind(
    key: str,
    label: str,
    group: str,
    unit: str,
    fn: Callable[[pd.DataFrame, int | None], pd.Series],
    *,
    takes_period: bool = False,
    default_period: int | None = None,
    description: str = "",
) -> IndicatorSpec:
    return IndicatorSpec(
        key=key,
        label=label,
        group=group,
        unit=unit,
        fn=fn,
        takes_period=takes_period,
        default_period=default_period,
        description=description,
    )


def _unavailable(key: str, label: str, group: str, unit: str, requires: str) -> IndicatorSpec:
    return IndicatorSpec(
        key=key,
        label=label,
        group=group,
        unit=unit,
        available=False,
        requires=requires,
        description=f"not collected yet — requires {requires}",
    )


# ---------------------------------------------------------------------------
# the catalog
# ---------------------------------------------------------------------------
INDICATOR_CATALOG: tuple[IndicatorSpec, ...] = (
    # --- price ------------------------------------------------------------
    _ind("close", "Close", "price", "currency", lambda df, p: _close(df),
         description="Last traded price (the newest cached close)"),
    _ind("open", "Open", "price", "currency", lambda df, p: df["open"].astype(float),
         description="Session open of the newest cached bar"),
    _ind("high", "High", "price", "currency", lambda df, p: df["high"].astype(float),
         description="Session high of the newest cached bar"),
    _ind("low", "Low", "price", "currency", lambda df, p: df["low"].astype(float),
         description="Session low of the newest cached bar"),
    _ind("prev_close", "Previous close", "price", "currency",
         lambda df, p: _shifted(df, "close", 1),
         description="Prior session's close — the reference every % change is measured against"),
    _ind("prev_high", "Previous day high", "previous-day", "currency",
         lambda df, p: _shifted(df, "high", 1),
         description="Yesterday's high — a close above it is a classic breakout test"),
    _ind("prev_low", "Previous day low", "previous-day", "currency",
         lambda df, p: _shifted(df, "low", 1),
         description="Yesterday's low — a close below it is a breakdown"),
    _ind("prev_open", "Previous day open", "previous-day", "currency",
         lambda df, p: _shifted(df, "open", 1),
         description="Yesterday's open — a prior-candle reference for gap and body tests"),
    _ind("prev_range_pct", "Previous day range %", "previous-day", "percent",
         lambda df, p: (
             (_shifted(df, "high", 1) - _shifted(df, "low", 1))
             / _shifted(df, "close", 1).replace(0, np.nan)
         ) * 100,
         description="Yesterday's high−low as a percentage of yesterday's close"),
    _ind("high_52w", "52-week high", "range", "currency",
         lambda df, p: _rolling_extreme(df, "high", int(p or 252), "max"),
         takes_period=True, default_period=252,
         description="Highest high of the trailing window (default 252 sessions)"),
    _ind("low_52w", "52-week low", "range", "currency",
         lambda df, p: _rolling_extreme(df, "low", int(p or 252), "min"),
         takes_period=True, default_period=252,
         description="Lowest low of the trailing window (default 252 sessions)"),
    _ind("from_52w_high_pct", "Below 52w high %", "range", "percent",
         lambda df, p: (
             _close(df) / _rolling_extreme(df, "high", int(p or 252), "max").replace(0, np.nan) - 1
         ) * 100,
         takes_period=True, default_period=252,
         description="0 means at the high; −10 means 10% below it"),
    _ind("from_52w_low_pct", "Above 52w low %", "range", "percent",
         lambda df, p: (
             _close(df) / _rolling_extreme(df, "low", int(p or 252), "min").replace(0, np.nan) - 1
         ) * 100,
         takes_period=True, default_period=252,
         description="0 means at the low; +25 means 25% above it"),
    # --- change -----------------------------------------------------------
    _ind("change_pct", "Change %", "change", "percent",
         lambda df, p: (_close(df) / _shifted(df, "close", 1).replace(0, np.nan) - 1) * 100,
         description="Close versus previous close, in percent"),
    _ind("change", "Change", "change", "currency",
         lambda df, p: _close(df) - _shifted(df, "close", 1),
         description="Close minus previous close, in rupees"),
    _ind(
        "gap_pct",
        "Gap %",
        "change",
        "percent",
        lambda df, p: (df["open"].astype(float) / _shifted(df, "close", 1).replace(0, np.nan) - 1) * 100,
        description="Today's open versus yesterday's close, in percent",
    ),
    _ind("ret_1w", "Return 1w %", "change", "percent",
         lambda df, p: (_close(df) / _close(df).shift(5).replace(0, np.nan) - 1) * 100,
         description="Close versus five sessions ago, in percent"),
    _ind("ret_1m", "Return 1m %", "change", "percent",
         lambda df, p: (_close(df) / _close(df).shift(22).replace(0, np.nan) - 1) * 100,
         description="Close versus 22 sessions ago, in percent"),
    _ind("ret_3m", "Return 3m %", "change", "percent",
         lambda df, p: (_close(df) / _close(df).shift(66).replace(0, np.nan) - 1) * 100,
         description="Close versus 66 sessions ago, in percent"),
    _ind("range_pct", "Range %", "change", "percent",
         lambda df, p: (
             (df["high"].astype(float) - df["low"].astype(float)) / _close(df).replace(0, np.nan)
         ) * 100,
         description="Today's high−low as a percentage of close"),
    _ind("close_in_range", "Close in day range %", "change", "percent",
         lambda df, p: (
             (_close(df) - df["low"].astype(float))
             / (df["high"].astype(float) - df["low"].astype(float)).replace(0, np.nan)
         ) * 100,
         description="0 = at the day's low, 100 = at the day's high"),
    # --- volume -----------------------------------------------------------
    _ind("volume", "Volume", "volume", "integer",
         lambda df, p: df["volume"].astype(float),
         description="Shares traded in the session"),
    _ind(
        "volume_sma",
        "Volume SMA",
        "volume",
        "number",
        lambda df, p: df["volume"].astype(float).rolling(int(p or 20), min_periods=1).mean(),
        takes_period=True,
        default_period=20,
        description="Average session volume over the trailing window",
    ),
    _ind(
        "rel_volume",
        "Relative volume",
        "volume",
        "multiple",
        lambda df, p: df["volume"].astype(float)
        / df["volume"].astype(float).shift(1).rolling(int(p or 20), min_periods=1).mean().replace(0, np.nan),
        takes_period=True,
        default_period=20,
        description="Today's volume ÷ the prior N-session average. "
        "The average excludes today, so one huge session cannot dilute its own ratio",
    ),
    _ind(
        "turnover",
        "Turnover",
        "volume",
        "currency",
        lambda df, p: _close(df) * df["volume"].astype(float),
        description="Close × volume, in rupees",
    ),
    _ind(
        "volume_change_pct",
        "Volume change %",
        "volume",
        "percent",
        lambda df, p: (
            df["volume"].astype(float) / df["volume"].astype(float).shift(1).replace(0, np.nan) - 1
        ) * 100,
        description="Today's volume versus yesterday's, in percent",
    ),
    # --- moving averages --------------------------------------------------
    _ind("sma", "SMA", "trend", "currency",
         lambda df, p: sma(_close(df), int(p or 50)),
         takes_period=True, default_period=50,
         description="Simple moving average of close over the window"),
    _ind("ema", "EMA", "trend", "currency",
         lambda df, p: ema(_close(df), int(p or 20)),
         takes_period=True, default_period=20,
         description="Exponential moving average of close — weights recent bars more"),
    _ind(
        "close_vs_sma_pct",
        "Close vs SMA %",
        "trend",
        "percent",
        lambda df, p: (
            _close(df) / sma(_close(df), int(p or 50)).replace(0, np.nan) - 1
        ) * 100,
        takes_period=True, default_period=50,
        description="How far close sits above (+) or below (−) its SMA, in percent",
    ),
    _ind(
        "close_vs_ema_pct",
        "Close vs EMA %",
        "trend",
        "percent",
        lambda df, p: (
            _close(df) / ema(_close(df), int(p or 20)).replace(0, np.nan) - 1
        ) * 100,
        takes_period=True, default_period=20,
        description="How far close sits above (+) or below (−) its EMA, in percent",
    ),
    # --- momentum ---------------------------------------------------------
    _ind("rsi", "RSI", "momentum", "number",
         lambda df, p: rsi(_close(df), int(p or 14)),
         takes_period=True, default_period=14,
         description="Wilder's RSI. Above 70 commonly read as overbought, below 30 oversold"),
    _ind(
        "rsi_change",
        "RSI change",
        "momentum",
        "number",
        lambda df, p: rsi(_close(df), int(p or 14)).diff(),
        takes_period=True, default_period=14,
        description="Change in RSI from the previous session — the direction, not the level",
    ),
    _ind(
        "dist_from_20d_high_pct",
        "Below 20d high %",
        "momentum",
        "percent",
        lambda df, p: (
            _close(df) / _rolling_extreme(df, "high", int(p or 20), "max").replace(0, np.nan) - 1
        ) * 100,
        takes_period=True, default_period=20,
        description="0 means at the recent high; −5 means 5% below it",
    ),
    # --- volatility -------------------------------------------------------
    _ind("atr", "ATR", "volatility", "currency",
         lambda df, p: _atr(df["high"], df["low"], _close(df), int(p or 14)),
         takes_period=True, default_period=14,
         description="Average true range in rupees — the absolute volatility figure"),
    _ind(
        "atr_pct",
        "ATR %",
        "volatility",
        "percent",
        lambda df, p: (
            _atr(df["high"], df["low"], _close(df), int(p or 14)) / _close(df).replace(0, np.nan)
        ) * 100,
        takes_period=True, default_period=14,
        description="True range as a percentage of price — the comparable volatility figure",
    ),
    _ind(
        "volatility_20d_pct",
        "Stdev 20d %",
        "volatility",
        "percent",
        lambda df, p: _close(df).pct_change().rolling(int(p or 20), min_periods=5).std() * 100,
        takes_period=True, default_period=20,
        description="Standard deviation of daily returns, in percent",
    ),
    # --- vwap -------------------------------------------------------------
    _ind(
        "vwap_20d",
        "VWAP 20d",
        "volume",
        "currency",
        lambda df, p: (
            (_close(df) * df["volume"].astype(float))
            .rolling(int(p or 20), min_periods=1).sum()
            / df["volume"].astype(float).rolling(int(p or 20), min_periods=1).sum().replace(0, np.nan)
        ),
        takes_period=True, default_period=20,
        description="Rolling volume-weighted average price. Daily bars carry one "
        "session each, so this is session-weighted over N days, not intraday VWAP",
    ),
    _ind(
        "close_vs_vwap_pct",
        "Close vs VWAP %",
        "volume",
        "percent",
        lambda df, p: (
            _close(df)
            / (
                (_close(df) * df["volume"].astype(float))
                .rolling(int(p or 20), min_periods=1).sum()
                / df["volume"].astype(float).rolling(int(p or 20), min_periods=1).sum().replace(0, np.nan)
            ).replace(0, np.nan)
            - 1
        ) * 100,
        takes_period=True, default_period=20,
        description="How far close sits above (+) or below (−) the rolling VWAP, in percent",
    ),
    # --- market structure -------------------------------------------------
    _ind("bars", "Sessions of history", "provenance", "integer",
         lambda df, p: pd.Series(float(len(df)), index=df.index),
         description="How many sessions are cached — a screen on a thin history is not evidence"),
    # --- declared but not computable in this build ------------------------
    _unavailable("market_cap", "Market cap", "fundamentals", "currency", "the fundamentals service"),
    _unavailable("pe", "P/E", "fundamentals", "number", "the fundamentals service"),
    _unavailable("pb", "P/B", "fundamentals", "number", "the fundamentals service"),
    _unavailable("eps", "EPS", "fundamentals", "currency", "the fundamentals service"),
    _unavailable("roe", "ROE", "fundamentals", "percent", "the fundamentals service"),
    _unavailable("promoter_holding", "Promoter %", "fundamentals", "percent", "the shareholding feed"),
)

INDICATOR_INDEX: dict[str, IndicatorSpec] = {spec.key: spec for spec in INDICATOR_CATALOG}

#: Every indicator a condition may actually name today.
AVAILABLE_INDICATORS: tuple[str, ...] = tuple(
    spec.key for spec in INDICATOR_CATALOG if spec.available
)


def resolve_indicator(key: str) -> IndicatorSpec:
    """Look up an indicator, with a useful error for an unknown one."""
    spec = INDICATOR_INDEX.get(str(key or "").strip().lower())
    if spec is None:
        known = ", ".join(sorted(INDICATOR_INDEX))
        raise KeyError(f"unknown indicator {key!r}. Known: {known}")
    return spec


def indicator_series(
    df: pd.DataFrame, key: str, period: int | None = None
) -> pd.Series:
    """The named indicator's series over *df*, or an all-NaN series."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    return resolve_indicator(key).series(df, period)


def last_value(series: pd.Series) -> float:
    """The newest finite value of a series, or NaN.

    NaN rather than ``None`` so callers can compare uniformly; every comparison
    against NaN is false, which is the behaviour a condition needs.
    """
    if series is None or len(series) == 0:
        return float("nan")
    value = series.iloc[-1]
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if math.isfinite(out) else float("nan")
