"""Custom condition-based scanner.

Evaluates user-defined conditions against the local Parquet cache — no broker
session required.  Each condition targets one indicator (LHS) compared to a
fixed value or another indicator (RHS) with a standard operator.

Supported indicators (LHS and RHS)
-----------------------------------
``close``, ``open``, ``high``, ``low``, ``volume``
``sma``  — period required (e.g. ``{"indicator": "sma", "period": 20}``)
``ema``  — period required
``rsi``  — period optional (default 14)
``atr``  — period optional (default 14)
``atr_pct``  — ATR as % of close
``bb_upper``, ``bb_mid``, ``bb_lower`` — period optional (default 20)
``vol_x``  — volume ÷ 20-day average volume
``day_chg_pct`` — (close/prev_close − 1) × 100
``ret_1m``  — 22-bar return %
``vs_high``  — (close / 63-bar high − 1) × 100

Operators
---------
``>``, ``<``, ``>=``, ``<=``, ``=``
``crosses_above``, ``crosses_below``  — true when the crossover happened on
the most-recent bar (LHS must be a time-series indicator, value is ignored —
set to 0).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from atr.strategy.indicators import atr as _atr, bollinger, ema, rsi as _rsi, sma


# ---------------------------------------------------------------------------
# Condition model (plain dict for JSON simplicity)
# ---------------------------------------------------------------------------
# {
#   "indicator": "rsi",          # LHS indicator name
#   "period":    14,              # LHS period (if applicable)
#   "op":        "<",             # operator
#   "rhs_type":  "value",         # "value" | "indicator"
#   "rhs_value": 35,              # used when rhs_type == "value"
#   "rhs_indicator": "sma",       # used when rhs_type == "indicator"
#   "rhs_period":    20,          # used when rhs_type == "indicator"
# }

_OPS = {
    ">":             lambda a, b: a > b,
    "<":             lambda a, b: a < b,
    ">=":            lambda a, b: a >= b,
    "<=":            lambda a, b: a <= b,
    "=":             lambda a, b: abs(a - b) < 1e-9,
}


def _series(df: pd.DataFrame, indicator: str, period: int | None) -> pd.Series:
    """Compute an indicator series from an OHLCV dataframe."""
    c = df["close"]
    p = period or 0

    if indicator == "close":      return c
    if indicator == "open":       return df["open"]
    if indicator == "high":       return df["high"]
    if indicator == "low":        return df["low"]
    if indicator == "volume":     return df["volume"].astype(float)
    if indicator == "sma":        return sma(c, p or 20)
    if indicator == "ema":        return ema(c, p or 20)
    if indicator == "rsi":        return _rsi(c, p or 14)
    if indicator == "atr":        return _atr(df["high"], df["low"], c, p or 14)
    if indicator == "atr_pct":
        a = _atr(df["high"], df["low"], c, p or 14)
        return (a / c.replace(0, np.nan)) * 100

    if indicator in ("bb_upper", "bb_mid", "bb_lower"):
        mid, upper, lower = bollinger(c, p or 20)
        if indicator == "bb_upper": return upper
        if indicator == "bb_mid":   return mid
        return lower

    if indicator == "vol_x":
        avg = df["volume"].shift(1).rolling(20).mean().replace(0, np.nan)
        return df["volume"].astype(float) / avg

    if indicator == "day_chg_pct":
        return (c / c.shift(1) - 1) * 100

    if indicator == "ret_1m":
        return (c / c.shift(22) - 1) * 100

    if indicator == "vs_high":
        hi = df["high"].rolling(63).max()
        return (c / hi.replace(0, np.nan) - 1) * 100

    raise ValueError(f"unknown indicator: {indicator!r}")


def _last(s: pd.Series) -> float:
    v = s.iloc[-1]
    return float(v) if math.isfinite(float(v)) else float("nan")


def eval_condition(df: pd.DataFrame, cond: dict[str, Any]) -> bool:
    """Return True if the condition holds on the last bar of *df*."""
    if len(df) < 60:
        return False
    op   = cond["op"]
    lhs_s = _series(df, cond["indicator"], cond.get("period"))

    if op in ("crosses_above", "crosses_below"):
        if len(lhs_s) < 2:
            return False
        rhs_s = _series(df, cond["rhs_indicator"], cond.get("rhs_period"))
        if op == "crosses_above":
            return bool(lhs_s.iloc[-1] > rhs_s.iloc[-1] and lhs_s.iloc[-2] <= rhs_s.iloc[-2])
        else:
            return bool(lhs_s.iloc[-1] < rhs_s.iloc[-1] and lhs_s.iloc[-2] >= rhs_s.iloc[-2])

    lhs_val = _last(lhs_s)
    if math.isnan(lhs_val):
        return False

    if cond.get("rhs_type") == "indicator":
        rhs_val = _last(_series(df, cond["rhs_indicator"], cond.get("rhs_period")))
    else:
        rhs_val = float(cond.get("rhs_value", 0))

    if math.isnan(rhs_val):
        return False

    fn = _OPS.get(op)
    if fn is None:
        raise ValueError(f"unknown operator: {op!r}")
    return bool(fn(lhs_val, rhs_val))


def indicator_value(df: pd.DataFrame, indicator: str, period: int | None) -> float:
    """Return the last value of an indicator for display in results."""
    try:
        return round(_last(_series(df, indicator, period)), 4)
    except Exception:  # noqa: BLE001
        return float("nan")


# ---------------------------------------------------------------------------
# Main scan function
# ---------------------------------------------------------------------------

def run_custom_scan(
    frames: dict[str, pd.DataFrame],
    conditions: list[dict[str, Any]],
    combine: str = "AND",  # "AND" | "OR"
) -> list[dict[str, Any]]:
    """Evaluate conditions over all cached frames.

    Returns a list of result dicts for matching symbols, each containing
    standard score_frame metrics plus the LHS indicator values from the
    conditions (for display).
    """
    from atr.scanner import score_frame

    results: list[dict[str, Any]] = []

    for symbol, df in frames.items():
        if len(df) < 60:
            continue
        try:
            # evaluate each condition
            hits = [eval_condition(df, c) for c in conditions]
            matched = all(hits) if combine == "AND" else any(hits)
            if not matched:
                continue

            row = score_frame(symbol, df)

            # append the LHS indicator values so the table can show them
            extra: dict[str, float] = {}
            for i, cond in enumerate(conditions):
                key = f"{cond['indicator']}{cond.get('period', '') or ''}_{i}"
                extra[key] = indicator_value(df, cond["indicator"], cond.get("period"))
            row["_cond_values"] = extra  # type: ignore[assignment]
            results.append(row)
        except Exception:  # noqa: BLE001
            continue

    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    return results
