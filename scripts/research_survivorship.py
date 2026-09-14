"""How much of the panel's return is survivorship bias?

The panel is 120 names selected by *today's* turnover from a cache of 2,672
symbols. That selection is look-ahead: the names that are liquid now are
disproportionately the names that did well. The result is that 97% of them rose
over the window and the equal-weight index compounded +480%, which is not a
believable number for Indian large caps over 2020-2026 and should not be
presented as one.

This script quantifies the damage so any backtest in this repo can state its
own bias rather than inherit it silently:

1. Rebuild the index from **every symbol in the cache** and compare it with the
   liquid-120 version. The gap is a lower bound on the survivorship effect.
2. Count how many cached symbols even have enough history, and how many are
   still listed / have not been delisted-and-refetched.
3. Report the same statistics for both, so a factor tested on either set can
   say which one it was tested on.

The point is not to fix survivorship -- the cache has no delisted names to add
back, so it cannot be fixed with the data on hand. The point is to make the
size of the problem visible, and to stop the liquid-120 index number from being
quoted as if it were the market.

Usage:
    ./.venv/Scripts/python.exe scripts/research_survivorship.py
"""

# ruff: noqa: I001
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

CACHE = Path("data/iifl_daily/NSEEQ")
PANEL = Path("data/signals/panel_nseeq_120.parquet")
MIN_BARS = 1000  # roughly four years of sessions


def long_history_symbols() -> pd.DataFrame:
    """Every cache symbol with a long enough history, as a close-price panel."""
    series = {}
    for path in sorted(CACHE.glob("*.parquet")):
        try:
            frame = pd.read_parquet(path, columns=["ts", "close"])
        except Exception:  # noqa: BLE001 — a bad file must not stop the scan
            continue
        if len(frame) < MIN_BARS:
            continue
        frame = frame.copy()
        frame["ts"] = pd.to_datetime(frame["ts"])
        series[path.stem.upper()] = frame.set_index("ts")["close"]
    return pd.DataFrame(series).sort_index()


def summarise(close: pd.DataFrame, label: str) -> dict:
    rets = close.pct_change()
    ew = rets.mean(axis=1).fillna(0)
    eq = (1 + ew).cumprod()
    total = (close.iloc[-1] / close.iloc[0] - 1) * 100
    out = {
        "label": label,
        "names": close.shape[1],
        "sessions": len(close),
        "index_total_pct": (eq.iloc[-1] - 1) * 100,
        "index_sharpe": ew.mean() / ew.std() * np.sqrt(252) if ew.std() > 0 else 0.0,
        "index_maxdd_pct": (eq / eq.cummax() - 1).min() * 100,
        "pct_names_positive": (total > 0).mean() * 100,
        "median_name_pct": total.median(),
        "p10_name_pct": total.quantile(0.10),
        "p90_name_pct": total.quantile(0.90),
    }
    return out


def main() -> int:
    print("building panels from the cache (this reads ~2,600 files)...")
    all_names = long_history_symbols()
    liquid = pd.read_parquet(PANEL)
    liquid["ts"] = pd.to_datetime(liquid["ts"])
    liquid_close = liquid.pivot_table(
        index="ts", columns="symbol", values="close"
    ).sort_index()

    rows = [summarise(all_names, "all cached w/ long history"),
            summarise(liquid_close, "liquid-120 panel")]

    # Align the two on the same dates before comparing, so the difference is
    # selection and not simply a different window.
    common = all_names.index.intersection(liquid_close.index)
    rows.append(summarise(all_names.loc[common], "all cached (aligned dates)"))
    rows.append(summarise(liquid_close.loc[common], "liquid-120 (aligned dates)"))

    header = (f"{'panel':<30} {'names':>6} {'total':>10} {'Sharpe':>7} "
              f"{'maxDD':>8} {'%pos':>6} {'median':>9}")
    print("\n" + header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['label']:<30} {r['names']:>6} "
              f"{r['index_total_pct']:>9.1f}% {r['index_sharpe']:>7.2f} "
              f"{r['index_maxdd_pct']:>7.1f}% {r['pct_names_positive']:>5.0f}% "
              f"{r['median_name_pct']:>8.0f}%")

    print("\n" + "=" * 72)
    aligned = [r for r in rows if "aligned" in r["label"]]
    if len(aligned) == 2:
        gap = aligned[1]["index_total_pct"] - aligned[0]["index_total_pct"]
        print("On identical dates:")
        print(f"  all-cached index    {aligned[0]['index_total_pct']:+.1f}%")
        print(f"  liquid-120 index    {aligned[1]['index_total_pct']:+.1f}%")
        print(f"  selection gap       {gap:+.1f} points")
        print()
        print("  Reading: the liquid-120 universe was chosen by today's turnover,")
        print("  so it is stacked with names that already worked. Every backtest on")
        print("  it -- strategy AND benchmark -- inherits that optimism. Comparisons")
        print("  between the two are still meaningful because both carry it; absolute")
        print("  return figures are not, and should never be quoted as 'the market'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
