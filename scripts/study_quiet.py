"""Does a stock that has gone quiet stay quiet? (Volatility persistence, not direction.)

Quiet = the last 5 sessions moved <=2% and their daily range shrank to <=70% of the previous
20 sessions. Measure the NEXT 5 sessions' absolute move, in units of that stock's own typical
5-day move (so a sleepy utility and a volatile small-cap are comparable), against all days.
Reported for an early and a late half of the history.

Run:  python scripts/study_quiet.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "data" / "iifl_daily" / "NSEEQ"


def main() -> None:
    rows = []
    for path in sorted(ROOT.glob("*.parquet")):
        try:
            df = pd.read_parquet(path, columns=["ts", "open", "high", "low", "close"])
        except Exception:
            continue
        if len(df) < 200:
            continue
        df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        c, h, lo = df["close"], df["high"], df["low"]
        span = h - lo
        ret5 = (c / c.shift(5) - 1) * 100
        typical = ret5.abs().rolling(120).mean().shift(5)  # the stock's own usual 5-day move
        fwd_abs = ((c.shift(-5) / c - 1) * 100).abs() / typical
        quiet = (ret5.abs() <= 2) & (span.rolling(5).mean() / span.rolling(20).mean() <= 0.7)
        top = c >= h.rolling(20).max() * 0.965
        rows.append(pd.DataFrame({"ts": df["ts"], "fwd": fwd_abs, "quiet": quiet, "top": top}).dropna())
    d = pd.concat(rows, ignore_index=True)
    cut = d["ts"].quantile(0.5)
    print("Next-5-session move as a multiple of the stock's usual 5-day move (1.00 = normal)\n")
    print(f"{'':32s} | {'early n':>8s} {'x usual':>8s} | {'late n':>8s} {'x usual':>8s}")
    for name, m in (("all days", d["fwd"].notna()), ("went quiet", d["quiet"]), ("went quiet AT THE TOP", d["quiet"] & d["top"])):
        e, l = d[m & (d["ts"] <= cut)], d[m & (d["ts"] > cut)]
        print(f"{name:32s} | {len(e):8,d} {e['fwd'].mean():8.2f} | {len(l):8,d} {l['fwd'].mean():8.2f}")
    q = d[d["quiet"] & d["top"]]
    print(f"\nAfter going quiet at the top, next-5-session move was under half its usual size "
          f"{100 * (q['fwd'] < 0.5).mean():.0f}% of the time, vs {100 * (d['fwd'] < 0.5).mean():.0f}% on all days.")


if __name__ == "__main__":
    sys.exit(main())
