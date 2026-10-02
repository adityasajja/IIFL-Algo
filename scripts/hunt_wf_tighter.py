"""Walk-forward validation with tighter SL configs."""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hunt_final3 import load_stocks, load_symbol, run_local_strategy, build_regime
from strategy_hunt import parse_universe

p = print
def P(*a, **kw): kw.setdefault("flush", True); p(*a, **kw)

def main():
    start = pd.Timestamp("2020-01-01")
    end = pd.Timestamp("2026-09-18")

    all_symbols = list(set(
        parse_universe(ROOT / "data" / "universe" / "n50.txt") +
        parse_universe(ROOT / "data" / "universe" / "mid150.txt")
    ))
    P(f"Loading {len(all_symbols)} symbols...")
    panels, syms, dates = load_stocks(all_symbols, start, end, min_bars=250)
    P(f"  {len(syms)} stocks, {len(dates)} dates")

    nift = load_symbol("NIFTYBEES", start, end)
    nc = nift["close"].reindex(dates).ffill().to_numpy(dtype=np.float64)
    r200 = build_regime(nc, 200)
    r50 = build_regime(nc, 50)
    reg_dual = r50 & r200
    reg_or = r50 | r200
    reg_200 = r200

    close = panels["close"].to_numpy(dtype=np.float64)
    high = panels["high"].to_numpy(dtype=np.float64)
    low = panels["low"].to_numpy(dtype=np.float64)
    open_ = panels["open"].to_numpy(dtype=np.float64)
    vol = panels["volume"].to_numpy(dtype=np.float64)
    n = len(dates)
    mid = n // 2

    gap = np.full_like(close, -np.inf)
    gap[1:] = (open_[1:] - close[:-1]) / close[:-1]
    gap = np.roll(gap, 1, axis=0); gap[:2] = -np.inf

    mom5 = np.full_like(close, -np.inf)
    mom5[5:] = close[5:] / close[:-5] - 1.0
    mom5 = np.roll(mom5, 1, axis=0); mom5[:6] = -np.inf

    vavg = np.full_like(vol, np.nan)
    for i in range(20, len(vol)):
        vavg[i] = vol[i - 20:i].mean(axis=0)
    vr = np.where(vavg > 0, vol / vavg, 0)

    gap_vol = np.where((gap > 0.015) & (vr > 1.0), gap * mom5 * vr, -np.inf)
    gap_run_5 = np.where(gap > 0.02, gap * mom5, -np.inf)

    fold_size = (n - mid) // 3

    P(f"\n{'='*70}")
    P(f"WALK-FORWARD: gap_vol / dual / tighter SL configs")
    P(f"{'='*70}")
    for label, sl, tp, mh, k in [
        ("dual_10_20_k5", 10, 20, 20, 5),
        ("dual_12_24_k5", 12, 24, 20, 5),
        ("dual_7_14_k5", 7, 14, 20, 5),
        ("dual_10_20_k3", 10, 20, 20, 3),
        ("dual_15_25_k5", 15, 25, 30, 5),
    ]:
        for fi in range(3):
            si = mid + fi * fold_size
            ei = mid + (fi + 1) * fold_size if fi < 2 else n
            r = run_local_strategy(
                close[si:ei], high[si:ei], low[si:ei], gap_vol[si:ei],
                sl, tp, mh, k, 5, costs=0.00283,
                regime_filter=reg_dual[si:ei], vol_target=0.2
            )
            if "error" in r:
                P(f"  {label} F{fi+1}: {r['error']}")
            else:
                P(f"  {label} F{fi+1}: wk={r['weekly_mean_pct']:+.3f}% S={r['sharpe']:.2f} "
                  f"DD={r['max_drawdown_pct']:.0f}% rb={r['n_rebalances']} win={r['weekly_win_rate_pct']:.0f}%")

    P(f"\n{'='*70}")
    P(f"WALK-FORWARD: gap_vol / 200d configs")
    P(f"{'='*70}")
    for label, sl, tp, mh, k in [
        ("200d_15_30_k3", 15, 30, 30, 3),
        ("200d_10_20_k5", 10, 20, 20, 5),
        ("200d_15_25_k5", 15, 25, 30, 5),
        ("200d_7_14_k5", 7, 14, 20, 5),
    ]:
        for fi in range(3):
            si = mid + fi * fold_size
            ei = mid + (fi + 1) * fold_size if fi < 2 else n
            r = run_local_strategy(
                close[si:ei], high[si:ei], low[si:ei], gap_vol[si:ei],
                sl, tp, mh, k, 5, costs=0.00283,
                regime_filter=reg_200[si:ei], vol_target=0.2
            )
            if "error" in r:
                P(f"  {label} F{fi+1}: {r['error']}")
            else:
                P(f"  {label} F{fi+1}: wk={r['weekly_mean_pct']:+.3f}% S={r['sharpe']:.2f} "
                  f"DD={r['max_drawdown_pct']:.0f}% rb={r['n_rebalances']} win={r['weekly_win_rate_pct']:.0f}%")

    P(f"\n{'='*70}")
    P(f"WALK-FORWARD: gap_run_5 / 200d (original recommendation)")
    P(f"{'='*70}")
    for label, sl, tp, mh, k in [
        ("gr5_15_25_k5", 15, 25, 30, 5),
        ("gr5_10_20_k5", 10, 20, 20, 5),
        ("gr5_7_14_k5", 7, 14, 20, 5),
        ("gr5_10_20_k3", 10, 20, 20, 3),
    ]:
        for fi in range(3):
            si = mid + fi * fold_size
            ei = mid + (fi + 1) * fold_size if fi < 2 else n
            r = run_local_strategy(
                close[si:ei], high[si:ei], low[si:ei], gap_run_5[si:ei],
                sl, tp, mh, k, 5, costs=0.00283,
                regime_filter=reg_200[si:ei], vol_target=0.2
            )
            if "error" in r:
                P(f"  {label} F{fi+1}: {r['error']}")
            else:
                P(f"  {label} F{fi+1}: wk={r['weekly_mean_pct']:+.3f}% S={r['sharpe']:.2f} "
                  f"DD={r['max_drawdown_pct']:.0f}% rb={r['n_rebalances']} win={r['weekly_win_rate_pct']:.0f}%")

    P("\nDone!")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
