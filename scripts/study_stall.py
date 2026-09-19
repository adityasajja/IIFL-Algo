"""Does the "stalled at the top" pattern precede weak stretches? Measured, not assumed.

For every stock in the local daily cache and every day, compute the detector's flag and
what happened over the next 5 and 10 sessions. Compare against three baselines:

* all days (what any stock does),
* ran up and sitting at the top but NOT stalled (still moving),
* the flagged days.

Run:  python scripts/study_stall.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atr.insights.stall import stall_frame  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "data" / "iifl_daily" / "NSEEQ"


def main() -> None:
    rows = []
    for path in sorted(ROOT.glob("*.parquet")):
        try:
            df = pd.read_parquet(path)
        except Exception:
            continue
        if len(df) < 90 or not {"open", "high", "low", "close", "ts"}.issubset(df.columns):
            continue
        f = stall_frame(df)
        c = f["close"]
        f["fwd5"] = (c.shift(-5) / c - 1) * 100
        f["fwd10"] = (c.shift(-10) / c - 1) * 100
        f["sym"] = path.stem
        rows.append(f.dropna(subset=["fwd5", "ext50", "ret20", "off_high", "range_ratio"]))
    data = pd.concat(rows, ignore_index=True)

    ran_top = ((data["ret20"] >= 10) | (data["ext50"] >= 8)) & (data["off_high"] >= -3.5)
    groups = {
        "all days": data,
        "ran up & at top, still moving": data[ran_top & ~data["gate"]],
        "gate (ran up, at top, flat)": data[data["gate"]],
        "FLAGGED (gate + score>=55)": data[data["flag"]],
    }
    print(f"{data['sym'].nunique()} stocks, {len(data):,} stock-days, "
          f"{data['ts'].min():%d %b %Y} to {data['ts'].max():%d %b %Y}\n")
    print(f"{'group':34s} {'n':>7s} | {'5d mean':>8s} {'5d med':>7s} {'5d down%':>9s} | {'10d mean':>9s} {'10d med':>8s}")
    for name, g in groups.items():
        g10 = g.dropna(subset=["fwd10"])
        print(
            f"{name:34s} {len(g):7,d} | {g['fwd5'].mean():8.2f} {g['fwd5'].median():7.2f} "
            f"{(g['fwd5'] < 0).mean() * 100:8.1f}% | {g10['fwd10'].mean():9.2f} {g10['fwd10'].median():8.2f}"
        )

    # The claim behind the alert: after a flag, does the stock fail to make progress?
    flagged = groups["FLAGGED (gate + score>=55)"]
    base = groups["all days"]
    for thr in (1.0, 2.0):
        print(
            f"\nWithin 5 sessions the stock gained less than {thr:.0f}%:  "
            f"flagged {(flagged['fwd5'] < thr).mean() * 100:.0f}%   vs all days {(base['fwd5'] < thr).mean() * 100:.0f}%"
        )
    print(f"Fell 3%+ within 5 sessions: flagged {(flagged['fwd5'] <= -3).mean() * 100:.0f}%   "
          f"vs all days {(base['fwd5'] <= -3).mean() * 100:.0f}%")
    # Stock-days overlap heavily (a stalled stock is flagged on consecutive days), so also
    # count distinct episodes: first flagged day per stock per 10-day stretch.
    ep = flagged.sort_values(["sym", "ts"]).copy()
    ep["gap"] = ep.groupby("sym")["ts"].diff().dt.days.fillna(999)
    first = ep[ep["gap"] > 10]
    print(f"\nDistinct episodes: {len(first):,}  mean 5d {first['fwd5'].mean():.2f}%  "
          f"median {first['fwd5'].median():.2f}%  down {(first['fwd5'] < 0).mean() * 100:.0f}%")


if __name__ == "__main__":
    main()
