"""Debug intraday signal detection."""
import sys, json
from pathlib import Path
from datetime import datetime
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

cache = ROOT / "data" / "iifl_1min"
symbols = ["RELIANCE", "HDFCBANK", "INFY", "ICICIBANK", "TCS",
           "HINDUNILVR", "BHARTIARTL", "KOTAKBANK", "WIPRO", "NIFTYBEES"]

df_dict = {}
for sym in symbols:
    f = cache / f"{sym}.parquet"
    if f.exists():
        df_dict[sym] = pd.read_parquet(f)

print(f"Symbols loaded: {list(df_dict.keys())}")
print(f"Total bars: {sum(len(d) for d in df_dict.values())}")

# Build daily from 1-min
daily = {}
for sym, df in df_dict.items():
    d = df.copy()
    d["date"] = d["ts"].dt.normalize()
    agg = d.groupby("date").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
    )
    daily[sym] = agg
    print(f"{sym}: {len(agg)} daily bars, {agg.index[0]} → {agg.index[-1]}")

all_dates = sorted(set(idx for d in daily.values() for idx in d.index))
print(f"Total daily dates: {len(all_dates)}")

# Build panels
close_daily = pd.DataFrame({s: d["close"].reindex(all_dates) for s, d in daily.items()}).ffill()
open_daily = pd.DataFrame({s: d["open"].reindex(all_dates) for s, d in daily.items()}).ffill()
vol_daily = pd.DataFrame({s: d["volume"].reindex(all_dates) for s, d in daily.items()}).ffill()

print(f"\nPanel shape: {close_daily.shape}")
print(f"Date range: {all_dates[0]} → {all_dates[-1]}")

# Compute gap
gap = (open_daily / close_daily.shift(1) - 1.0)
print(f"\nGap stats: mean={gap.mean().mean():.4f}, max={gap.max().max():.4f}")

# Vol ratio
vol_avg = vol_daily.rolling(20).mean()
vol_ratio = vol_daily / vol_avg.replace(0, np.nan)
print(f"Vol ratio stats: mean={vol_ratio.mean().mean():.2f}, median={vol_ratio.median().median():.2f}")

# Momentum
mom5 = close_daily / close_daily.shift(5) - 1.0
print(f"Mom5 stats: mean={mom5.mean().mean():.4f}")

# Check NIFTY regime
if "NIFTYBEES" in daily:
    n_close = close_daily["NIFTYBEES"]
    n_sma200 = n_close.rolling(200).mean()
    regime_ok = n_close > n_sma200
    print(f"Regime: {regime_ok.sum()}/{len(regime_ok)} days bull ({100*regime_ok.mean():.0f}%)")

# Count gap-up signals
signal_mask = (gap >= 0.5/100.0) & (vol_ratio >= 1.0) & (mom5 > 0)
print(f"\nSignal mask (gap>=0.5%, vol>=1.0, mom>0): {signal_mask.sum().sum()} total signals")

# Try with lower threshold
signal_mask_0 = (gap >= 0.0/100.0) & (mom5 > 0)
print(f"Signal mask (gap>=0%, mom>0): {signal_mask_0.sum().sum()} total signals")

# Per-symbol gap-up counts
for sym in close_daily.columns:
    if sym == "NIFTYBEES":
        continue
    n_gap = (gap[sym] >= 0.5/100.0).sum()
    n_vol = (vol_ratio[sym] >= 1.0).sum()
    n_mom = (mom5[sym] > 0).sum()
    n_all = signal_mask[sym].sum()
    print(f"  {sym}: gap>0.5%={n_gap}, vol>1.0={n_vol}, mom>0={n_mom}, all3={n_all}")

# Show some gap-up days
print("\nSample gap-up days (>1%):")
for i in range(20, len(all_dates)):
    day_gap = gap.iloc[i]
    big_gaps = day_gap[day_gap > 0.01].sort_values(ascending=False)
    if len(big_gaps) > 0:
        print(f"  {all_dates[i].date()}: {['{}:{:+.1%}'.format(s, g) for s, g in big_gaps.head(3).items()]}")
