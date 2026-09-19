"""Run the fixed oversold-and-volatile rule on stocks it was never developed on.

The pattern was found among 341 long-history stocks. The same folder holds ~2,700 more
with about a year of bars each. None of them entered any earlier study, so this is a
true holdout in *names* as well as in time, with nothing tuned to it: the rule is
applied exactly as written (RSI(14) < 30 and 20-day volatility in the top fifth of that
week's cross-section, outcome = next-5-bar gain of 2% or more).

Illiquid names can print wide, unreachable moves, so the result is shown both for every
stock and for those trading at least Rs 2 crore a day.

Run:  ./.venv/Scripts/python.exe scripts/study_unseen_stocks.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from atr.research.forward_tracker import RSI_BELOW, VOL_QUANTILE, _rsi
from atr.research.hunt import STOCKS, STOCK_COSTS, stock_universe

HIT = 0.02
ROUND_TRIP = STOCK_COSTS.round_trip_pct() / 100
MIN_BARS = 200
LIQUID_CRORE = 2.0


def main() -> None:
    seen = set(stock_universe(min_bars=1500))
    close_cols, value_cols = [], []
    for path in STOCKS.glob("*-EQ.parquet"):
        base = path.stem[:-3]
        if base in seen:
            continue  # the same company under its other spelling was in the earlier studies
        df = pd.read_parquet(path, columns=["ts", "close", "volume"]).dropna()
        df = df[df["close"] > 0].drop_duplicates("ts")
        if len(df) < MIN_BARS:
            continue
        df = df.set_index("ts")
        close_cols.append(df["close"].rename(base))
        value_cols.append((df["close"] * df["volume"]).rename(base))
    close = pd.concat(close_cols, axis=1, sort=True)
    value = pd.concat(value_cols, axis=1, sort=True)
    print(f"{close.shape[1]} stocks never used before, {len(close)} sessions "
          f"({close.index[0].date()} to {close.index[-1].date()})")

    ret = close.pct_change()
    rsi = close.apply(_rsi)
    vol = ret.rolling(20).std()
    liquid = value.rolling(20).median() >= LIQUID_CRORE * 1e7
    fwd = close.shift(-5) / close - 1

    weeks = list(close.groupby(close.index.to_period("W-FRI")).tail(1).index)
    rows = []
    for d in weeks:
        v, r, f, lq = vol.loc[d], rsi.loc[d], fwd.loc[d], liquid.loc[d]
        ok = v.notna() & r.notna() & f.notna()
        if ok.sum() < 200:
            continue
        cut = v[ok].quantile(VOL_QUANTILE)
        flag = ok & (r < RSI_BELOW) & (v >= cut)
        for s in flag[flag].index:
            rows.append({"date": d, "symbol": s, "fwd": f[s], "hit": float(f[s] >= HIT), "liquid": bool(lq[s])})
        rows.append({"date": d, "symbol": "*base*", "fwd": float(f[ok].mean()), "hit": float((f[ok] >= HIT).mean()), "liquid": True})
    frame = pd.DataFrame(rows)
    base = frame[frame["symbol"] == "*base*"]["hit"].mean()
    sig = frame[frame["symbol"] != "*base*"]
    print(f"ordinary stock-week rate in this universe: {100 * base:.1f}%\n")

    def show(tag: str, part: pd.DataFrame) -> None:
        if len(part) < 30:
            print(f"{tag:<26} only {len(part)} signals")
            return
        n, hits = len(part), float(part["hit"].sum())
        p = hits / n
        z = 1.96
        centre = (p + z * z / (2 * n)) / (1 + z * z / n)
        half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        wk = part.groupby("date")["hit"].mean()
        print(f"{tag:<26} n={n:>5}  hit {100 * p:>5.1f}%  (likely {100 * (centre - half):.0f}-{100 * (centre + half):.0f}%)  "
              f"weeks {len(wk):>3}  week-weighted {100 * wk.mean():.1f}%  net avg {100 * (part['fwd'].mean() - ROUND_TRIP):+.2f}%/wk  "
              f"median {100 * part['fwd'].median():+.2f}%")

    show("all stocks", sig)
    show("liquid (>= Rs 2cr/day)", sig[sig["liquid"]])
    show("illiquid", sig[~sig["liquid"]])
    top = sig.groupby("date").size().sort_values(ascending=False)
    print(f"\nbusiest weeks hold {100 * top.head(3).sum() / top.sum():.0f}% of signals: "
          + ", ".join(f"{d.date()} n={n}" for d, n in top.head(3).items()))
    ex = sig[~sig["date"].isin(top.head(3).index)]
    show("excluding those 3 weeks", ex)


if __name__ == "__main__":
    main()
