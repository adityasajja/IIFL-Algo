"""Pivot points — Classic (floor-trader), Fibonacci, and Camarilla — computed
from the previous completed session's high/low/close.

Pivot levels are the one item on this list that's less a general TA concept
and more an *Indian intraday trading convention* specifically: they're a
daily fixture on NSE/BSE trading desks and retail terminals (Zerodha's
Pivot Point Calculator, most Indian charting platforms default to showing
them) in a way that's less universal in, say, US equity trading culture. All
three flavours share the same pivot point (PP); they differ only in how the
support/resistance levels are spaced out from it.

Works for both a daily frame (one bar per session — "previous day" is simply
the bar before the last one) and an intraday frame (many bars per session —
grouped by calendar date, "previous day" is the last *complete* group before
today's).
"""

from __future__ import annotations

import pandas as pd


def _previous_session_hlc(frame: pd.DataFrame) -> tuple[float, float, float] | None:
    """(high, low, close) of the last *completed* session before the current
    one, or None without at least two sessions' worth of bars.
    """
    if frame is None or len(frame) < 2:
        return None
    if "ts" not in frame.columns:
        # No real timestamps (e.g. a synthetic forming-bar frame in tests) —
        # fall back to treating each row as its own session.
        prev = frame.iloc[-2]
        return float(prev["high"]), float(prev["low"]), float(prev["close"])

    ts = pd.to_datetime(frame["ts"], errors="coerce")
    if ts.isna().all():
        prev = frame.iloc[-2]
        return float(prev["high"]), float(prev["low"]), float(prev["close"])

    dates = ts.dt.date
    unique_dates = dates.drop_duplicates().to_numpy()
    if len(unique_dates) < 2:
        return None
    prev_date = unique_dates[-2]
    session = frame.loc[dates == prev_date]
    if session.empty:
        return None
    return float(session["high"].max()), float(session["low"].min()), float(session["close"].iloc[-1])


def pivot_point_context(frame: pd.DataFrame) -> dict[str, float]:
    """Classic, Fibonacci, and Camarilla pivot levels from the previous
    session's H/L/C. All 0.0 without at least two sessions of history.
    """
    names = [
        "pivot", "pivot_r1", "pivot_r2", "pivot_r3", "pivot_s1", "pivot_s2", "pivot_s3",
        "fib_pivot_r1", "fib_pivot_r2", "fib_pivot_r3", "fib_pivot_s1", "fib_pivot_s2", "fib_pivot_s3",
        "camarilla_r1", "camarilla_r2", "camarilla_r3", "camarilla_r4",
        "camarilla_s1", "camarilla_s2", "camarilla_s3", "camarilla_s4",
        "cpr_pivot", "cpr_bc", "cpr_tc", "cpr_width_pct",
        "prev_day_high", "prev_day_low", "prev_day_close",
    ]
    zero = dict.fromkeys(names, 0.0)

    hlc = _previous_session_hlc(frame)
    if hlc is None:
        return zero
    h, l, c = hlc
    rng = h - l
    pp = (h + l + c) / 3.0

    out = dict(zero)
    out["pivot"] = round(pp, 4)
    # --- Classic (floor-trader) ---
    out["pivot_r1"] = round(2 * pp - l, 4)
    out["pivot_s1"] = round(2 * pp - h, 4)
    out["pivot_r2"] = round(pp + rng, 4)
    out["pivot_s2"] = round(pp - rng, 4)
    out["pivot_r3"] = round(h + 2 * (pp - l), 4)
    out["pivot_s3"] = round(l - 2 * (h - pp), 4)
    # --- Fibonacci ---
    out["fib_pivot_r1"] = round(pp + 0.382 * rng, 4)
    out["fib_pivot_s1"] = round(pp - 0.382 * rng, 4)
    out["fib_pivot_r2"] = round(pp + 0.618 * rng, 4)
    out["fib_pivot_s2"] = round(pp - 0.618 * rng, 4)
    out["fib_pivot_r3"] = round(pp + 1.0 * rng, 4)
    out["fib_pivot_s3"] = round(pp - 1.0 * rng, 4)
    # --- Camarilla --- (anchored on the close, not the pivot, by design)
    out["camarilla_r1"] = round(c + rng * 1.1 / 12.0, 4)
    out["camarilla_r2"] = round(c + rng * 1.1 / 6.0, 4)
    out["camarilla_r3"] = round(c + rng * 1.1 / 4.0, 4)
    out["camarilla_r4"] = round(c + rng * 1.1 / 2.0, 4)
    out["camarilla_s1"] = round(c - rng * 1.1 / 12.0, 4)
    out["camarilla_s2"] = round(c - rng * 1.1 / 6.0, 4)
    out["camarilla_s3"] = round(c - rng * 1.1 / 4.0, 4)
    out["camarilla_s4"] = round(c - rng * 1.1 / 2.0, 4)

    # --- CPR (Central Pivot Range) --- the three-line pivot variant most
    # associated with Indian intraday trading education specifically: BC
    # (bottom central) and TC (top central) bracket the pivot, and how wide
    # that bracket is relative to price is itself a commonly-traded signal
    # (a "narrow CPR" is read as set up for a trending day, a "wide CPR" for
    # a range-bound one) — hence `cpr_width_pct` alongside the three levels.
    bc = (h + l) / 2.0
    tc = pp + (pp - bc)
    cpr_lo, cpr_hi = (bc, tc) if bc <= tc else (tc, bc)
    out["cpr_pivot"] = round(pp, 4)
    out["cpr_bc"] = round(bc, 4)
    out["cpr_tc"] = round(tc, 4)
    out["cpr_width_pct"] = round((cpr_hi - cpr_lo) / pp * 100.0, 4) if pp else 0.0

    out["prev_day_high"] = round(h, 4)
    out["prev_day_low"] = round(l, 4)
    out["prev_day_close"] = round(c, 4)
    return out


#: name -> meaning, same author-facing contract as the other indicator modules.
PIVOT_POINT_NAMES: dict[str, str] = {
    "pivot": "the classic floor-trader pivot point: (prev high + prev low + prev close) / 3",
    "pivot_r1": "classic resistance 1", "pivot_r2": "classic resistance 2", "pivot_r3": "classic resistance 3",
    "pivot_s1": "classic support 1", "pivot_s2": "classic support 2", "pivot_s3": "classic support 3",
    "fib_pivot_r1": "Fibonacci pivot resistance 1 (pivot + 38.2% of prev range)",
    "fib_pivot_r2": "Fibonacci pivot resistance 2 (pivot + 61.8% of prev range)",
    "fib_pivot_r3": "Fibonacci pivot resistance 3 (pivot + 100% of prev range)",
    "fib_pivot_s1": "Fibonacci pivot support 1", "fib_pivot_s2": "Fibonacci pivot support 2", "fib_pivot_s3": "Fibonacci pivot support 3",
    "camarilla_r1": "Camarilla resistance 1 (tightest, most-watched intraday level)",
    "camarilla_r2": "Camarilla resistance 2", "camarilla_r3": "Camarilla resistance 3 (a common breakout trigger)",
    "camarilla_r4": "Camarilla resistance 4 (widest)",
    "camarilla_s1": "Camarilla support 1", "camarilla_s2": "Camarilla support 2",
    "camarilla_s3": "Camarilla support 3 (a common breakdown trigger)", "camarilla_s4": "Camarilla support 4 (widest)",
    "cpr_pivot": "CPR pivot — same value as `pivot`, included under the CPR name for convenience",
    "cpr_bc": "CPR bottom central — (prev high + prev low) / 2",
    "cpr_tc": "CPR top central — the pivot's mirror of BC, on the other side of the pivot",
    "cpr_width_pct": "width of the CPR band as a % of the pivot — narrow is read as a trending-day setup, wide as range-bound",
    "prev_day_high": "previous session's high", "prev_day_low": "previous session's low", "prev_day_close": "previous session's close",
}
