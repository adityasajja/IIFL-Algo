"""Buy at Monday's open, sell the moment it touches +2%, with a stop and a Friday exit.

The earlier studies asked whether a stock *closed* the week 2% higher. The plan being
tested here is different and easier to hit: enter Monday, place a sell at +2%, and take
it whenever the price touches that at any point in the week. The catch is what happens
when it does not: without a stop, a stock that never bounces is simply held, and the
loss is hidden. So every trade also has a stop, and a Friday-close exit.

Fills are modelled on daily bars, pessimistically:
* entry at Monday's open;
* if a later day opens beyond the target or the stop, the fill is that open (a gap);
* if one day's range touches both the stop and the target, the stop is assumed first;
* a round trip costs 0.33% (statutory plus slippage) and is subtracted from every trade.

Run:  ./.venv/Scripts/python.exe scripts/study_intraweek_target.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atr.data.hygiene import reverting_spike_mask
from atr.research.hunt import STOCK_COSTS, STOCKS, stock_universe

TARGET = 0.02
ROUND_TRIP = STOCK_COSTS.round_trip_pct() / 100
DAYS = 5


def weeks_for(symbol: str) -> pd.DataFrame | None:
    """One row per week: entry open, then five days of open/high/low/close, plus signals known before Monday."""
    df = pd.read_parquet(STOCKS / f"{symbol}.parquet", columns=["ts", "open", "high", "low", "close"]).dropna()
    df = df[(df["close"] > 0) & (df["open"] > 0)].drop_duplicates("ts").reset_index(drop=True)
    df = df[df["ts"] >= "2015-01-01"].reset_index(drop=True)
    if len(df) < 400:
        return None
    df = df[~reverting_spike_mask(df["close"]).to_numpy()].reset_index(drop=True)
    week = df["ts"].dt.to_period("W-FRI")
    first = df.index[week != week.shift(1)].to_numpy()
    first = first[(first >= 30) & (first + DAYS <= len(df))]
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    idx = first[:, None] + np.arange(DAYS)[None, :]
    close = pd.Series(c)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14).mean()
    rsi = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).to_numpy()
    vol = close.pct_change().rolling(20).std().to_numpy()
    prev = first - 1  # the Friday close before the week: the last thing known at entry
    ema50 = close.ewm(span=50).mean().to_numpy()
    hi252 = close.rolling(252, min_periods=60).max().to_numpy()
    return pd.DataFrame(
        {
            "symbol": symbol, "date": df["ts"].to_numpy()[first], "entry": o[first],
            **{f"o{k}": o[idx[:, k]] for k in range(DAYS)}, **{f"h{k}": h[idx[:, k]] for k in range(DAYS)},
            **{f"l{k}": l[idx[:, k]] for k in range(DAYS)}, "close_end": c[idx[:, DAYS - 1]],
            "rsi": rsi[prev], "vol20": vol[prev],
            "ret5": c[prev] / c[prev - 5] - 1, "ret20": c[prev] / c[prev - 20] - 1,
            "dist_ema50": c[prev] / ema50[prev] - 1, "dist_52w_high": c[prev] / hi252[prev] - 1,
            "gap_open": o[first] / c[prev] - 1,
        }
    )


def simulate(w: pd.DataFrame, stop: float | None, target: float = TARGET) -> pd.DataFrame:
    """Exit price and reason for every week, day by day."""
    entry = w["entry"].to_numpy()
    tgt = entry * (1 + target)
    stp = entry * (1 - stop) if stop else np.full(len(w), -np.inf)
    exit_px = np.full(len(w), np.nan)
    reason = np.array([""] * len(w), dtype=object)
    live = np.ones(len(w), bool)
    day = np.full(len(w), DAYS - 1)
    for k in range(DAYS):
        o, h, l = w[f"o{k}"].to_numpy(), w[f"h{k}"].to_numpy(), w[f"l{k}"].to_numpy()
        if k > 0:  # gaps: only possible after the entry day
            gap_up, gap_dn = live & (o >= tgt), live & (o <= stp)
            exit_px[gap_up], reason[gap_up] = o[gap_up], "target"
            exit_px[gap_dn], reason[gap_dn] = o[gap_dn], "stop"
            day[gap_up | gap_dn] = k
            live &= ~(gap_up | gap_dn)
        hit_stop, hit_tgt = live & (l <= stp), live & (h >= tgt)
        stopped = hit_stop  # stop wins a tie: the pessimistic reading of a bar that touched both
        exit_px[stopped], reason[stopped] = stp[stopped], "stop"
        won = hit_tgt & ~stopped
        exit_px[won], reason[won] = tgt[won], "target"
        day[stopped | won] = k
        live &= ~(stopped | won)
    exit_px[live], reason[live] = w["close_end"].to_numpy()[live], "friday"
    out = w[["symbol", "date", "rsi", "vol20", "ret5", "ret20", "dist_ema50", "dist_52w_high", "gap_open"]].copy()
    out["ret"] = exit_px / entry - 1
    out["day"] = day  # 0 = Monday, the day of entry
    out["reason"] = reason
    return out


def report(tag: str, r: pd.DataFrame) -> None:
    n = len(r)
    if n < 200:
        return
    net = r["ret"] - ROUND_TRIP
    print(f"{tag:<34}{n:>8,}  target {100 * (r.reason == 'target').mean():>5.1f}%  stop {100 * (r.reason == 'stop').mean():>5.1f}%  "
          f"friday {100 * (r.reason == 'friday').mean():>5.1f}%  winners {100 * (net > 0).mean():>5.1f}%  "
          f"avg net {100 * net.mean():>+6.2f}%  median {100 * net.median():>+6.2f}%")


def main() -> None:
    frames = [w for w in (weeks_for(s) for s in stock_universe(min_bars=1500)) if w is not None]
    weeks = pd.concat(frames, ignore_index=True)
    print(f"{len(weeks):,} stock-weeks across {weeks['symbol'].nunique()} stocks, {weeks['date'].min().date()} to {weeks['date'].max().date()}\n")
    print(f"{'exit plan (sell at +2%)':<34}{'trades':>8}\n")
    for stop in (None, 0.10, 0.05, 0.03, 0.02):
        report("no stop" if stop is None else f"stop at -{int(stop * 100)}%", simulate(weeks, stop))

    print("\nSame plan (stop -3%) split by how jumpy the stock is (20-day volatility):")
    base = simulate(weeks, 0.03)
    q = pd.qcut(base["vol20"], 5, labels=["calmest", "calm", "mid", "jumpy", "jumpiest"])
    for name in ("calmest", "calm", "mid", "jumpy", "jumpiest"):
        report(f"  {name} fifth", base[q == name])

    print("\nAnd for the oversold pattern (RSI < 30 the Friday before, top-fifth volatility):")
    cut = base["vol20"].quantile(0.8)
    for stop in (0.05, 0.03):
        r = simulate(weeks, stop)
        report(f"  oversold + volatile, stop -{int(stop * 100)}%", r[(r["rsi"] < 30) & (r["vol20"] >= cut)])
    mid = weeks["date"].sort_values().iloc[len(weeks) // 2]
    b3 = simulate(weeks, 0.03)
    print("\nEarly versus late half (stop -3%):")
    report("  all stocks, early half", b3[b3["date"] < mid])
    report("  all stocks, late half", b3[b3["date"] >= mid])


if __name__ == "__main__":
    main()
