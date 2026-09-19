"""Does any chart pattern raise the odds of a 2%+ week, in both halves of the history?

Every Friday, for every stock, note which patterns are present at the close and
record whether the *next* week gained 2% or more. A pattern is only interesting if
it lifts that probability above what the stock's own volatility already explains,
in both the early and the late half, and survives the fact that dozens were tried.

Why volatility is controlled for: a stock that moves 6% a week will clear +2% far
more often than one that moves 1.5%, whatever pattern it shows. Without that control
the study would "discover" that jumpy stocks have more big weeks.

The stock universe is today's long-history names, so failed companies are absent.
That inflates absolute levels; the comparisons here are within one universe.

Run:  ./.venv/Scripts/python.exe scripts/study_chart_patterns.py
"""

from __future__ import annotations

import json
from math import sqrt
from statistics import NormalDist

import numpy as np
import pandas as pd

from atr.research.hunt import STOCK_COSTS, STOCKS, stock_universe

OUT = STOCKS.parents[2] / "research" / "chart_patterns.json"
HIT = 0.02  # the weekly gain being asked for
ROUND_TRIP = STOCK_COSTS.round_trip_pct() / 100


def features(close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, vol: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Boolean pattern flags per stock per day, using only data up to that close."""
    ret = close.pct_change()
    ema20, ema50 = close.ewm(span=20).mean(), close.ewm(span=50).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    vol_ratio = vol.rolling(5).mean() / vol.rolling(20).mean()
    hi52 = close.rolling(252).max()
    hi20 = close.shift(1).rolling(20).max()
    span10 = (high.rolling(10).max() - low.rolling(10).min()) / close
    gap_up = (close.pct_change() > 0.02)
    return {
        "new_20d_high": close > hi20,
        "within_3pct_of_52w_high": close >= 0.97 * hi52,
        "new_52w_high": close >= hi52,
        "uptrend_ema20>ema50": (close > ema20) & (ema20 > ema50),
        "below_ema50": close < ema50,
        "rsi_below_30": rsi < 30,
        "rsi_above_70": rsi > 70,
        "rsi_50_to_65": (rsi > 50) & (rsi < 65),
        "volume_spike_2x": vol_ratio > 2.0,
        "up_gt_2pct_day_this_week": gap_up.rolling(5).max().astype(bool),
        "tight_10d_range_lt5pct": span10 < 0.05,
        "down_10pct_in_4w": close / close.shift(20) - 1 < -0.10,
        "up_10pct_in_4w": close / close.shift(20) - 1 > 0.10,
        "up_3pct_last_week": close / close.shift(5) - 1 > 0.03,
        "down_3pct_last_week": close / close.shift(5) - 1 < -0.03,
        "breakout_on_volume": (close > hi20) & (vol_ratio > 1.5),
        "squeeze_then_breakout": (span10.shift(1) < 0.05) & (close > hi20),
    }, ret.rolling(20).std() * np.sqrt(5)  # expected weekly move, from the last month


def build() -> tuple[pd.DataFrame, dict[str, pd.Series], object]:
    """Stock-weeks with their outcome, one boolean pattern column each, and the midpoint date."""
    names = stock_universe(min_bars=1500)
    frames = {}
    for kind, column in (("close", "close"), ("high", "high"), ("low", "low"), ("volume", "volume")):
        cols = []
        for s in names:
            df = pd.read_parquet(STOCKS / f"{s}.parquet", columns=["ts", column]).dropna()
            df = df[df[column] > 0].drop_duplicates("ts").set_index("ts")[column].rename(s)
            cols.append(df)
        frames[kind] = pd.concat(cols, axis=1, sort=True).loc["2015-01-01":]
    close, high, low, volume = frames["close"], frames["high"], frames["low"], frames["volume"]
    # Drop the corrupt-bar spikes the broker cache carries, so they cannot fake huge weeks.
    from atr.data.hygiene import reverting_spike_mask

    for s in close.columns:
        series = close[s]
        present = series.dropna()
        bad = reverting_spike_mask(present).to_numpy()
        close.loc[present.index[bad], s] = np.nan

    flags, weekly_move = features(close, high, low, volume)
    # Each week's last trading day. Broker bars are stamped 09:15, so calendar
    # Friday midnight labels never appear in the index.
    fridays = list(close.groupby(close.index.to_period("W-FRI")).tail(1).index)
    nxt = close.shift(-5)
    fwd = (nxt / close - 1).loc[fridays]
    move = weekly_move.loc[fridays]

    # A stock-week counts only when both the flags and the outcome are defined.
    valid = fwd.notna() & move.notna()
    stacked = pd.DataFrame({"fwd": fwd.where(valid).stack(), "move": move.where(valid).stack()})
    stacked["hit"] = stacked["fwd"] >= HIT
    stacked["date"] = stacked.index.get_level_values(0)
    bucket = pd.qcut(stacked["move"], 5, labels=False, duplicates="drop")
    stacked["vol_bucket"] = bucket
    # What volatility alone predicts: the hit rate of stocks that move about this much.
    stacked["expected_hit"] = stacked.groupby("vol_bucket")["hit"].transform("mean")
    mid = stacked["date"].sort_values().iloc[len(stacked) // 2]
    columns = {
        name: flag.loc[fridays].where(valid).stack().reindex(stacked.index).fillna(False).astype(bool)
        for name, flag in flags.items()
    }
    return stacked, columns, mid


def main() -> None:
    stacked, columns, mid = build()
    base = float(stacked["hit"].mean())
    flags = columns
    n_tests = len(flags)
    z_needed = NormalDist().inv_cdf(1 - 0.05 / (2 * n_tests))  # Bonferroni over the patterns tried

    rows = []
    for name, flag in flags.items():
        f = flag
        sel = stacked[f]
        if len(sel) < 500:
            continue

        def measure(part: pd.DataFrame) -> dict:
            n = len(part)
            if n < 100:
                return {"n": n}
            p = float(part["hit"].mean())
            exp = float(part["expected_hit"].mean())
            se = sqrt(max(exp * (1 - exp), 1e-9) / n)
            net = float(part["fwd"].mean() - ROUND_TRIP)
            return {
                "n": n,
                "p_hit": round(100 * p, 2),
                "p_hit_expected_from_volatility": round(100 * exp, 2),
                "lift_pts": round(100 * (p - exp), 2),
                "z_vs_volatility_only": round((p - exp) / se, 2),
                "mean_next_week_net_pct": round(100 * net, 3),
            }

        early, late = sel[sel["date"] < mid], sel[sel["date"] >= mid]
        full = measure(sel)
        e, l = measure(early), measure(late)
        both = (
            e.get("lift_pts", -9) > 0 and l.get("lift_pts", -9) > 0
            and abs(full.get("z_vs_volatility_only", 0)) >= z_needed
        )
        rows.append({"pattern": name, "full": full, "early": e, "late": l, "lift_in_both_halves_and_significant": bool(both)})

    rows.sort(key=lambda r: -r["full"].get("lift_pts", -99))
    result = {
        "question": f"Does a chart pattern raise the chance the next week gains >= {int(HIT * 100)}%?",
        "base_rate_pct_all_stock_weeks": round(100 * base, 2),
        "hit_rate_by_volatility_quintile_pct": {
            int(k): round(100 * v, 2) for k, v in stacked.groupby("vol_bucket")["hit"].mean().items()
        },
        "stock_weeks": int(len(stacked)),
        "patterns_tested": n_tests,
        "bonferroni_z_required": round(z_needed, 2),
        "round_trip_cost_pct": round(100 * ROUND_TRIP, 3),
        "survivorship_note": "universe is today's long-history names; failed companies are absent",
        "results": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=1), encoding="utf8")

    print(f"stock-weeks {len(stacked):,}   base rate P(next week >= {int(HIT * 100)}%) = {result['base_rate_pct_all_stock_weeks']}%")
    print("by volatility quintile (low->high):", result["hit_rate_by_volatility_quintile_pct"])
    print(f"\n{'pattern':<28}{'n':>8}{'P(hit)':>8}{'vol-only':>9}{'lift':>7}{'z':>7}{'early':>7}{'late':>7}{'net%/wk':>9}  robust")
    for r in rows:
        f = r["full"]
        print(
            f"{r['pattern']:<28}{f['n']:>8,}{f['p_hit']:>8}{f['p_hit_expected_from_volatility']:>9}{f['lift_pts']:>7}"
            f"{f['z_vs_volatility_only']:>7}{r['early'].get('lift_pts', ''):>7}{r['late'].get('lift_pts', ''):>7}"
            f"{f['mean_next_week_net_pct']:>9}  {'YES' if r['lift_in_both_halves_and_significant'] else ''}"
        )


if __name__ == "__main__":
    main()
