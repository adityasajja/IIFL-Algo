"""Which buy setups and which market conditions have historical evidence behind them?

Wide matrices over every cached stock (dates x symbols), so market-wide measures (breadth,
the equal-weight market return, relative strength) come from the same data. Every result
is reported for an early and a late half of the history; only something that holds in both
should be trusted.

Run:  python scripts/study_buy_and_market.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "data" / "iifl_daily" / "NSEEQ"


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    close, high, vol = {}, {}, {}
    for path in sorted(ROOT.glob("*.parquet")):
        try:
            df = pd.read_parquet(path, columns=["ts", "close", "high", "volume"])
        except Exception:
            continue
        if len(df) < 300:
            continue
        df = df.drop_duplicates("ts").set_index("ts").sort_index()
        close[path.stem], high[path.stem], vol[path.stem] = df["close"], df["high"], df["volume"]
    return pd.DataFrame(close).sort_index(), pd.DataFrame(high).sort_index(), pd.DataFrame(vol).sort_index()


def summarise(name, mask, fwd5, fwd20, base5, base20, period):
    m = mask & period
    n = int(m.sum().sum())
    if n < 300:
        return f"{name:34s} n={n:,}  (too few)"
    f5, f20 = fwd5[m].stack(), fwd20[m].stack()
    b5, b20 = base5[period.iloc[:, 0]].stack().mean(), base20[period.iloc[:, 0]].stack().mean()
    return (
        f"{name:34s} n={n:8,d} | 5d {f5.mean():+5.2f}% (edge {f5.mean() - b5:+5.2f}) "
        f"| 20d {f20.mean():+5.2f}% (edge {f20.mean() - b20:+5.2f}) hit {100 * (f20 > 0).mean():4.1f}%"
    )


def main() -> None:
    close, high, vol = load()
    print(f"{close.shape[1]} stocks, {close.index.min():%b %Y} to {close.index.max():%b %Y}\n")
    ret1 = close.pct_change() * 100
    ret20 = (close / close.shift(20) - 1) * 100
    fwd5 = (close.shift(-5) / close - 1) * 100
    fwd20 = (close.shift(-20) / close - 1) * 100
    sma50 = close.rolling(50).mean()
    above50 = close > sma50
    hi252 = high.rolling(252, min_periods=120).max()
    off252 = (close / hi252 - 1) * 100
    rvol = vol / vol.rolling(20).mean().shift(1)
    prior_hi20 = high.rolling(20).max().shift(1)

    mkt20 = ret20.mean(axis=1)
    rs = ret20.sub(mkt20, axis=0)  # relative strength against the equal-weight market
    breadth = above50.where(close.notna()).mean(axis=1) * 100

    valid = close.notna() & fwd5.notna() & fwd20.notna() & ret20.notna() & sma50.notna() & off252.notna()
    base5 = fwd5.where(valid)
    base20 = fwd20.where(valid)
    cut = close.index[int(len(close.index) * 0.5)]
    early = pd.DataFrame(np.repeat((close.index <= cut)[:, None], close.shape[1], axis=1), index=close.index, columns=close.columns)
    late = ~early

    setups = {
        "all stock-days (baseline)": valid,
        "breakout: new 20d high + 1.5x vol": valid & (close > prior_hi20) & (rvol >= 1.5),
        "leader resting (pullback in RS>=10)": valid & (rs >= 10) & above50 & (off252 >= -12) & (off252 <= -3) & (ret1 >= -3) & (ret1 <= 1),
        "leader at highs (RS>=15, <=3% off)": valid & (rs >= 15) & above50 & (off252 >= -3),
        "strong 20d run (RS>=25)": valid & (rs >= 25),
        "oversold (down 20% in 20d)": valid & (ret20 <= -20),
    }
    for label, period in (("EARLY half", early), ("LATE half ", late)):
        print(f"--- {label}")
        for name, mask in setups.items():
            print(summarise(name, mask, fwd5, fwd20, base5, base20, period))
        print()

    # Market scenario: what has followed a given state of breadth? (equal-weight market)
    m5 = fwd5.where(close.notna()).mean(axis=1)
    m20 = fwd20.where(close.notna()).mean(axis=1)
    table = pd.DataFrame({"breadth": breadth, "m5": m5, "m20": m20}).dropna()
    table["half"] = np.where(table.index <= cut, "early", "late")
    bins = [0, 25, 35, 50, 65, 101]
    labels = ["<25%", "25-35%", "35-50%", "50-65%", ">65%"]
    table["bucket"] = pd.cut(table["breadth"], bins=bins, labels=labels, right=False)
    print("Market: equal-weight return over the NEXT 20 sessions by today's breadth (% above 50-day avg)")
    print(f"{'breadth':8s} | {'early days':>10s} {'20d avg':>8s} | {'late days':>10s} {'20d avg':>8s}")
    for lab in labels:
        e = table[(table["bucket"] == lab) & (table["half"] == "early")]
        l = table[(table["bucket"] == lab) & (table["half"] == "late")]
        print(f"{lab:8s} | {len(e):10,d} {e['m20'].mean():+8.2f} | {len(l):10,d} {l['m20'].mean():+8.2f}")
    print(f"{'all':8s} | {int((table['half'] == 'early').sum()):10,d} {table[table['half'] == 'early']['m20'].mean():+8.2f} | "
          f"{int((table['half'] == 'late').sum()):10,d} {table[table['half'] == 'late']['m20'].mean():+8.2f}")


if __name__ == "__main__":
    sys.exit(main())
