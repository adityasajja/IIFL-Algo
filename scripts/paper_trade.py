"""Paper trading monitor for the gap-and-run strategy.

Runs daily checks using cached daily data:
1. Checks NIFTY regime (50d AND 200d SMA)
2. If bullish, applies the gap_vol signal and selects top-k stocks
3. Outputs trade suggestions (entries and exits)
4. Saves results to data/research/paper_trade_log.json

Usage:
    python scripts/paper_trade.py              # Run once
    python scripts/paper_trade.py --loop      # Run daily (checks at 9:30 AM IST)
"""
import sys, json, argparse
from pathlib import Path
from datetime import datetime, date

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hunt_final3 import load_stocks, run_local_strategy, build_regime
from strategy_hunt import parse_universe

DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
LOG = ROOT / "data" / "research" / "paper_trade_log.json"
TRADING_DAYS = 252
COST = 0.00283


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run daily monitor")
    parser.add_argument("--start", default="2023-05-11", help="OOS start date")
    parser.add_argument("--end", default="2026-09-18", help="End date")
    args = parser.parse_args()

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)

    # Load universe
    all_symbols = list(dict.fromkeys(
        parse_universe(ROOT / "data" / "universe" / "n50.txt") +
        parse_universe(ROOT / "data" / "universe" / "mid150.txt")
    ))
    if "NIFTYBEES" not in all_symbols:
        all_symbols.append("NIFTYBEES")

    P(f"Loading {len(all_symbols)} symbols for paper trading monitor...")
    # Load FULL data (2020-2026) so regime SMAs are correct, then slice at OOS
    full_start = pd.Timestamp("2020-01-01")
    full_end = pd.Timestamp(args.end)
    panels, syms, full_dates = load_stocks(all_symbols, full_start, full_end, min_bars=250)

    if len(syms) < 10:
        P("ERROR: Insufficient data")
        return 1

    # Find OOS start index
    oos_start_idx = 0
    for i, d in enumerate(full_dates):
        if d >= start:
            oos_start_idx = i
            break
    P(f"Loaded: {len(syms)} stocks, {len(full_dates)} total dates "
      f"(OOS from {full_dates[oos_start_idx].date()})")

    close = panels["close"].to_numpy(dtype=np.float64)
    high = panels["high"].to_numpy(dtype=np.float64)
    low = panels["low"].to_numpy(dtype=np.float64)
    open_ = panels["open"].to_numpy(dtype=np.float64)
    volume = panels["volume"].to_numpy(dtype=np.float64)

    # NIFTY regime (using original hunt_final3 functions)
    nidx = syms.index("NIFTYBEES") if "NIFTYBEES" in syms else -1
    if nidx >= 0:
        n_close = close[:, nidx]
        n_regime_200 = build_regime(n_close, 200)
        n_regime_50 = build_regime(n_close, 50)
        n_regime_dual = n_regime_50 & n_regime_200
        last = len(n_close) - 1
        sma50 = float(np.nanmean(n_close[max(0, last-49):last+1]))
        sma200 = float(np.nanmean(n_close[max(0, last-199):last+1]))
        regime_info = {
            "current_price": float(n_close[last]),
            "sma50": sma50,
            "sma200": sma200,
            "above_50d": bool(n_regime_50[last]),
            "above_200d": bool(n_regime_200[last]),
            "dual_regime": bool(n_regime_dual[last]),
            "regime_days": int(n_regime_dual.sum()),
            "total_days": int(len(n_close)),
            "pct_regime_days": round(100 * n_regime_dual.sum() / len(n_close), 1),
        }
    else:
        regime_info = {"current_price": None, "dual_regime": False}
        n_regime_dual = None

    # Compute gap_vol signal (matching hunt_final3.py exactly)
    c = close; o = open_; v = volume
    gap = np.full_like(c, -np.inf)
    gap[1:] = (o[1:] - c[:-1]) / c[:-1]
    gap = np.roll(gap, 1, axis=0); gap[:2] = -np.inf

    mom_5d = np.full_like(c, -np.inf)
    mom_5d[5:] = c[5:] / c[:-5] - 1.0
    mom_5d = np.roll(mom_5d, 1, axis=0); mom_5d[:6] = -np.inf

    vol_avg = np.full_like(v, np.nan)
    for i in range(20, len(v)):
        vol_avg[i] = v[i - 20:i].mean(axis=0)
    vr = np.where(vol_avg > 0, v / vol_avg, 0)

    gap_vol = np.where((gap > 0.015) & (vr > 1.0), gap * mom_5d * vr, -np.inf)

    P(f"\n{'='*80}")
    P("PAPER TRADING MONITOR - gap_and_run strategy")
    P(f"{'='*80}")
    P(f"\nRegime Check (as of {full_dates[-1].strftime('%Y-%m-%d')}):")
    rp = regime_info.get('current_price', 0)
    if rp:
        P(f"  NIFTY Price:  {rp:.2f}")
    s50 = regime_info.get('sma50')
    if s50 is not None and not (isinstance(s50, float) and np.isnan(s50)):
        P(f"  50d SMA:     {s50:.2f} ({'ABOVE' if regime_info['above_50d'] else 'BELOW'})")
    s200 = regime_info.get('sma200')
    if s200 is not None and not (isinstance(s200, float) and np.isnan(s200)):
        P(f"  200d SMA:    {s200:.2f} ({'ABOVE' if regime_info['above_200d'] else 'BELOW'})")
    P(f"  Dual Regime: {'BULL' if regime_info['dual_regime'] else 'BEAR/SIDEWAYS'}")
    if regime_info.get('regime_days') is not None:
        P(f"  Regime coverage: {regime_info['regime_days']}/{regime_info['total_days']} days "
          f"({regime_info['pct_regime_days']}%)")

    # Run strategy on OOS slice (signals/regime computed on full data)
    oos_dates = full_dates[oos_start_idx:]
    r = run_local_strategy(
        close[oos_start_idx:], high[oos_start_idx:], low[oos_start_idx:],
        gap_vol[oos_start_idx:],
        sl_pct=10, tp_pct=20, max_holding=20, k=3, step=5,
        costs=COST, regime_filter=n_regime_dual[oos_start_idx:], vol_target=0.20,
    )

    P(f"\nPaper Trading Results (gap_vol/dual/sl10/tp20/mh20/k3/vt0.2):")
    P(f"  Period: {oos_dates[0].strftime('%Y-%m-%d')} → {oos_dates[-1].strftime('%Y-%m-%d')}")
    if "error" in r:
        P(f"  Result: {r['error']}")
        if "n_rebalances" in r:
            P(f"  Rebalances: {r['n_rebalances']}")
    else:
        for k_key, v_val in r.items():
            P(f"  {k_key}: {v_val}")

    # Also run the other recommended config
    r2 = run_local_strategy(
        close[oos_start_idx:], high[oos_start_idx:], low[oos_start_idx:],
        gap_vol[oos_start_idx:],
        sl_pct=15, tp_pct=30, max_holding=30, k=3, step=5,
        costs=COST, regime_filter=n_regime_dual[oos_start_idx:], vol_target=0.20,
    )
    P(f"\nAlternative Config (gap_vol/dual/sl15/tp30/mh30/k3/vt0.2):")
    if "error" in r2:
        P(f"  Result: {r2['error']}")
    else:
        for k_key, v_val in r2.items():
            P(f"  {k_key}: {v_val}")

    # Save log
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "period": [str(oos_dates[0].date()), str(oos_dates[-1].date())],
        "regime": regime_info,
        "config_1": {"name": "gap_vol/dual/sl10/tp20/mh20/k3/vt0.2", **r},
        "config_2": {"name": "gap_vol/dual/sl15/tp30/mh30/k3/vt0.2", **r2},
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logs = []
    if LOG.exists():
        try:
            logs = json.loads(LOG.read_text())
        except Exception:
            logs = []
    logs.append(log_entry)
    LOG.write_text(json.dumps(logs, indent=2, default=str), encoding="utf-8")
    P(f"\nLog saved to {LOG}")

    # Recommendation
    P(f"\n{'='*80}")
    P("RECOMMENDATION:")
    P(f"{'='*80}")
    if regime_info["dual_regime"]:
        P("  ✓ Market is in BULL regime. Strategy is ACTIVE.")
        P("  → Apply gap_vol signal, select top-3 stocks, enter with SL/TP")
    else:
        P("  ✗ Market is in BEAR/SIDEWAYS regime. Strategy is INACTIVE.")
        P("  → Keep portfolio in cash. Wait for dual regime to turn positive.")
        P(f"  → Last bull day: {regime_info.get('last_bull_date', 'search logs')}")

    return 0


def P(*a, **k):
    k.setdefault("flush", True)
    print(*a, **k)


if __name__ == "__main__":
    raise SystemExit(main())
