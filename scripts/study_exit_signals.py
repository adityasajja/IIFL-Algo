"""Which "this stock is topping" conditions, if any, precede weak stretches?

Population: stock-days where the stock ran up (>=10% in 20 sessions or >=8% over its 50-day
average) and sits within 3.5% of its 20-day high. Within it, test single conditions and
report the average next-5-session return against the population's own average, separately
for an early and a late half of the history, so a result has to hold in both to count.

Run:  python scripts/study_exit_signals.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atr.strategy.indicators import rsi  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "data" / "iifl_daily" / "NSEEQ"


def features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("ts").reset_index(drop=True)
    c, h, lo, o = (df[k].astype(float) for k in ("close", "high", "low", "open"))
    v = df["volume"].astype(float) if "volume" in df.columns else pd.Series(0.0, index=df.index)
    span = (h - lo).replace(0, np.nan)
    ema10 = c.ewm(span=10, adjust=False).mean()
    r = rsi(c)
    out = pd.DataFrame({"ts": df["ts"]})
    out["ret20"] = (c / c.shift(20) - 1) * 100
    out["ext50"] = (c / c.rolling(50).mean() - 1) * 100
    out["off_high"] = (c / h.rolling(20).max() - 1) * 100
    out["ret5"] = (c / c.shift(5) - 1) * 100
    out["flat"] = (out["ret5"].abs() <= 2.5) & (span.rolling(5).mean() / span.rolling(20).mean() <= 0.9)
    out["rsi_roll"] = (r.shift(5) >= 65) & (r.shift(5) - r >= 4)
    out["vol_dry"] = (v.rolling(5).mean() / v.rolling(20).mean().replace(0, np.nan)) <= 0.85
    out["wicks"] = ((h - np.maximum(o, c)) / span).rolling(5).mean() >= 0.35
    out["big_run"] = out["ret20"] >= 25
    out["below_5d_low"] = c < lo.shift(1).rolling(5).min()  # broke the recent range
    out["below_ema10"] = c < ema10
    out["down_2d"] = (c < c.shift(1)) & (c.shift(1) < c.shift(2))
    out["gap_down"] = o < c.shift(1) * 0.985
    out["fwd5"] = (c.shift(-5) / c - 1) * 100
    out["ran_top"] = ((out["ret20"] >= 10) | (out["ext50"] >= 8)) & (out["off_high"] >= -3.5)
    return out


def main() -> None:
    frames = []
    for path in sorted(ROOT.glob("*.parquet")):
        try:
            df = pd.read_parquet(path)
        except Exception:
            continue
        if len(df) < 90 or not {"open", "high", "low", "close", "ts"}.issubset(df.columns):
            continue
        f = features(df)
        f["sym"] = path.stem
        frames.append(f.dropna(subset=["fwd5", "ret20", "off_high"]))
    d = pd.concat(frames, ignore_index=True)
    pop = d[d["ran_top"]].copy()
    cut = pop["ts"].quantile(0.5)
    early, late = pop[pop["ts"] <= cut], pop[pop["ts"] > cut]
    print(f"population: {len(pop):,} stock-days that ran up and sit at the top; split at {cut:%b %Y}")
    print(f"average next-5d return of the population: early {early['fwd5'].mean():.2f}%  late {late['fwd5'].mean():.2f}%\n")
    print(f"{'condition':26s} | {'early n':>8s} {'early 5d':>9s} {'edge':>6s} | {'late n':>8s} {'late 5d':>9s} {'edge':>6s} | verdict")
    for col in ("flat", "rsi_roll", "vol_dry", "wicks", "big_run", "below_5d_low", "below_ema10", "down_2d", "gap_down"):
        e, l = early[early[col]], late[late[col]]
        ee = e["fwd5"].mean() - early["fwd5"].mean()
        le = l["fwd5"].mean() - late["fwd5"].mean()
        verdict = "WEAK in both" if ee < -0.3 and le < -0.3 else ("strong in both" if ee > 0.3 and le > 0.3 else "no consistent edge")
        print(f"{col:26s} | {len(e):8,d} {e['fwd5'].mean():9.2f} {ee:+6.2f} | {len(l):8,d} {l['fwd5'].mean():9.2f} {le:+6.2f} | {verdict}")


if __name__ == "__main__":
    main()
