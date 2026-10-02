"""Fast focused search: OOS validation of top configs + small-cap sweep.
Pre-loads data once to avoid repeated parquet reads."""
from __future__ import annotations
import json, sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import strategy_hunt as sh
from atr.research.hunt import STOCK_COSTS, buy_and_hold, run_weights, compare

DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
OUT = ROOT / "data" / "research"

p = print  # alias for quick typing

def P(*a, **k):
    k.setdefault("flush", True)
    p(*a, **k)


def build_panel_cached(symbols, start, end, min_bars=250):
    P(f"  Loading {len(symbols)} symbols...")
    panel = sh.build_universe_panel(symbols, start, end, min_bars=min_bars)
    P(f"  Panel: {panel.shape[1]} stocks, {panel.shape[0]} bars")
    return panel


def test_oos_top_configs(panel, sig_configs, oos_start):
    """Test top Phase 1 configs on OOS period. Pre-compute signals once."""
    P(f"\n{'='*72}")
    P(f"OUT-OF-SAMPLE VALIDATION")
    P(f"{'='*72}")

    mid = len(panel) // 2
    # Use the Phase 1 train/test split midpoint
    oos_idx = panel.index.searchsorted(oos_start)
    if oos_idx == 0 or oos_idx >= len(panel):
        oos_idx = mid
    oos_start_dt = panel.index[oos_idx]

    train_panel = panel.iloc[:oos_idx]
    oos_panel = panel.iloc[oos_idx:]

    P(f"  Train: {train_panel.index[0].date()} → {train_panel.index[-1].date()} ({len(train_panel)} bars)")
    P(f"  OOS:   {oos_panel.index[0].date()} → {oos_panel.index[-1].date()} ({len(oos_panel)} bars)")

    # Benchmark
    bh_w = sh._equal_weight_hold(oos_panel, oos_panel.index[0])
    bh = run_weights(oos_panel, bh_w, STOCK_COSTS)
    bh_m = bh.metrics()
    P(f"\n  B&H (OOS): CAGR={bh_m['cagr_pct']:.1f}% Sharpe={bh_m['sharpe']:.3f} "
      f"weekly={bh_m['weekly_mean_pct']:+.4f}% DD={bh_m['max_drawdown_pct']:.1f}%")

    # Pre-compute signals on full panel
    invvol_full = 1.0 / panel.pct_change().rolling(63, min_periods=40).std()
    invvol_full = invvol_full.replace([np.inf, -np.inf], np.nan)

    # Pre-compute OHLC
    ohlc = sh._load_ohlc_for_panel(panel.columns.tolist(), panel.index)
    ohlc_note = " (OHLC loaded)" if ohlc else ""

    results = []
    for cfg in sig_configs:
        name = cfg["name"]
        fn = cfg["fn"]
        kw = cfg["kwargs"]
        k = cfg["k"]
        step = cfg["step"]
        sizing = cfg.get("sizing", "equal")
        needs_ohlc = cfg.get("needs_ohlc", False)

        try:
            if needs_ohlc:
                P(f"\n  Computing {name} (OHLC-based)...", )
                sig = fn(ohlc[0], ohlc[1], ohlc[4], **kw)  # close, open, volume
            else:
                sig = fn(panel, **kw)

            # Train
            train_sig = sig.loc[sig.index <= train_panel.index[-1]]
            train_invvol = invvol_full.loc[invvol_full.index <= train_panel.index[-1]]
            train_w = sh._build_weights(train_panel, train_sig, train_invvol,
                                        train_panel.index[0], train_panel.index[-1],
                                        k, step, sizing)
            if train_w.empty:
                continue
            train_result = run_weights(train_panel, train_w, STOCK_COSTS)
            train_m = train_result.metrics()

            # OOS
            oos_sig = sig.loc[sig.index > train_panel.index[-1]]
            oos_invvol = invvol_full.loc[invvol_full.index > train_panel.index[-1]]
            oos_w = sh._build_weights(oos_panel, oos_sig, oos_invvol,
                                        oos_panel.index[0], oos_panel.index[-1],
                                        k, step, sizing)
            if oos_w.empty:
                continue
            oos_result = run_weights(oos_panel, oos_w, STOCK_COSTS)
            oos_m = oos_result.metrics()
            if oos_m["years"] < 0.5:
                continue

            P(f"\n  {name}/k={k}/step={step}/{sizing}:")
            P(f"    Train: CAGR={train_m['cagr_pct']:.1f}% Sharpe={train_m['sharpe']:.3f} "
              f"wk={train_m['weekly_mean_pct']:+.3f}% DD={train_m['max_drawdown_pct']:.1f}%")
            P(f"    OOS:   CAGR={oos_m['cagr_pct']:.1f}% Sharpe={oos_m['sharpe']:.3f} "
              f"wk={oos_m['weekly_mean_pct']:+.3f}% DD={oos_m['max_drawdown_pct']:.1f}%")
            P(f"    P(>=5%/wk)={sh._weekly_hit_rate(oos_result, 0.05)}% "
              f"P(>=2%/wk)={oos_m['weeks_ge_2pct_pct']}% "
              f"cost_drag={oos_m['cost_drag_pct_yr']:.2f}%/yr")

            results.append({
                "name": name, "k": k, "step": step, "sizing": sizing,
                "train": sh._slim_metrics(train_m),
                "oos": sh._slim_metrics(oos_m),
                "oos_p_ge_5pct": sh._weekly_hit_rate(oos_result, 0.05),
                "decay": round(oos_m["weekly_mean_pct"] - train_m["weekly_mean_pct"], 4),
            })
        except Exception as e:
            P(f"  {name}/k={k}/step={step}/{sizing}: ERROR: {e}")

    return results


def smallcap_sweep(panel, k_vals=(2, 3, 5), steps=(1, 5, 10)):
    """Sweep short-horizon signals on a small-cap panel."""
    P(f"\n{'='*72}")
    P(f"SMALL-CAP SWEEP")
    P(f"{'='*72}")

    mid = len(panel) // 2
    train_panel = panel.iloc[:mid + 252]
    train_end = train_panel.index[-1]
    invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
    invvol = invvol.replace([np.inf, -np.inf], np.nan)

    bh_w = sh._equal_weight_hold(train_panel, train_panel.index[0])
    bh = run_weights(train_panel, bh_w, STOCK_COSTS)
    bh_m = bh.metrics()
    P(f"\n  B&H: CAGR={bh_m['cagr_pct']:.1f}% Sharpe={bh_m['sharpe']:.3f} "
      f"weekly={bh_m['weekly_mean_pct']:+.3f}% DD={bh_m['max_drawdown_pct']:.1f}%")

    signals = [
        ("mom_1d", sh.signal_momentum, {"lookback": 1}),
        ("mom_2d", sh.signal_momentum, {"lookback": 2}),
        ("mom_3d", sh.signal_momentum, {"lookback": 3}),
        ("mom_5d", sh.signal_momentum, {"lookback": 5}),
        ("mom_10d", sh.signal_momentum, {"lookback": 10}),
        ("mom_1d_vol", sh.signal_composite_mom_vol, {"mom_lookback": 1, "vol_window": 20}),
        ("mom_3d_vol", sh.signal_composite_mom_vol, {"mom_lookback": 3, "vol_window": 20}),
        ("rev_3d_10d", sh.signal_mean_reversion, {"lookback": 3, "vol_window": 10}),
        ("rev_5d_20d", sh.signal_mean_reversion, {"lookback": 5, "vol_window": 20}),
    ]

    records = []
    for sig_name, fn, kw in signals:
        try:
            sig = fn(train_panel, **kw)
        except Exception:
            continue
        for k in k_vals:
            for step in steps:
                for sizing in ["equal", "invvol"]:
                    try:
                        weights = sh._build_weights(train_panel, sig, invvol,
                                                    train_panel.index[0], train_end,
                                                    k, step, sizing)
                        if weights.empty or weights.sum().sum() == 0:
                            continue
                        result = run_weights(train_panel, weights, STOCK_COSTS)
                        m = result.metrics()
                        if m["years"] < 1:
                            continue
                        ctrl_w = sh._build_random_weights(train_panel, train_panel.index[0],
                                                        train_end, k, step, seed=42)
                        ctrl_result = run_weights(train_panel, ctrl_w, STOCK_COSTS)
                        cm = ctrl_result.metrics()
                        records.append({
                            "signal": sig_name, "k": k, "step": step, "sizing": sizing,
                            "cagr_pct": m["cagr_pct"], "sharpe": m["sharpe"],
                            "weekly_mean_pct": m["weekly_mean_pct"],
                            "max_dd_pct": m["max_drawdown_pct"],
                            "turnover_per_yr": m["turnover_per_yr"],
                            "cost_drag_pct_yr": m["cost_drag_pct_yr"],
                            "weeks_ge_2pct_pct": m["weeks_ge_2pct_pct"],
                            "weekly_p_ge_5pct": sh._weekly_hit_rate(result, 0.05),
                            "excess_sharpe": round(m["sharpe"] - cm["sharpe"], 3),
                        })
                    except Exception:
                        pass

    records.sort(key=lambda r: r["weekly_mean_pct"], reverse=True)
    P(f"\n  {len(records)} valid configs")
    if records:
        for r in records[:10]:
            P(f"  {r['signal']}/k{r['k']}/step{r['step']}/{r['sizing']}: "
              f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
              f"CAGR={r['cagr_pct']:.0f}% ex={r['excess_sharpe']:+.3f} "
              f"DD={r['max_dd_pct']:.0f}% P(>=5%)={r['weekly_p_ge_5pct']}%")

    return records[:15]


# ---------------------------------------------------------------------------
# Combined momentum + gap + volume signal
# ---------------------------------------------------------------------------
def combined_mom_gap_signal(panel, ohlc, mom_lb, gap_w):
    """Composite: rank by momentum and gap, average ranks for higher conviction."""
    mom = (panel / panel.shift(mom_lb) - 1.0).shift(1)
    closes, opens, _, _, volumes = ohlc
    gap = (opens / closes.shift(1) - 1.0).shift(1)
    vol_ratio = volumes / volumes.shift(5).rolling(5, min_periods=3).mean()
    gap_score = gap * np.sqrt(vol_ratio.clip(upper=10))
    mom_rank = mom.rank(axis=1, ascending=False, method="average")
    gap_rank = gap_score.rank(axis=1, ascending=False, method="average")
    combined = (mom_rank + gap_rank) / 2
    return -combined  # lower rank = better → positive score


def run_combined_sweep(panel, ohlc, mom_lookbacks=(2, 3, 5, 10), gap_windows=(1, 2, 3)):
    """Test combined momentum+gap signal."""
    P(f"\n{'='*72}")
    P(f"COMBINED MOMENTUM + GAP SIGNAL")
    P(f"{'='*72}")

    mid = len(panel) // 2
    train_panel = panel.iloc[:mid + 252]
    train_end = train_panel.index[-1]
    invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
    invvol = invvol.replace([np.inf, -np.inf], np.nan)

    bh_w = sh._equal_weight_hold(train_panel, train_panel.index[0])
    bh = run_weights(train_panel, bh_w, STOCK_COSTS)
    bh_m = bh.metrics()
    P(f"\n  B&H: CAGR={bh_m['cagr_pct']:.1f}% Sharpe={bh_m['sharpe']:.3f} "
      f"weekly={bh_m['weekly_mean_pct']:+.3f}%")

    # Pre-extract OHLC for train panel
    train_ohlc = None
    if ohlc is not None:
        train_ohlc = tuple(x.loc[x.index <= train_end] for x in ohlc)

    records = []
    for mlb in mom_lookbacks:
        for gw in gap_windows:
            if train_ohlc is None:
                continue
            try:
                sig = combined_mom_gap_signal(train_panel, train_ohlc, mlb, gw)
            except Exception as e:
                P(f"  combined mom{mlb}_gap{gw}: ERROR: {e}")
                continue
            for k in [2, 3, 5]:
                for step in [5, 10]:
                    try:
                        weights = sh._build_weights(train_panel, sig, invvol,
                                                    train_panel.index[0], train_end,
                                                    k, step, "equal")
                        if weights.empty or weights.sum().sum() == 0:
                            continue
                        result = run_weights(train_panel, weights, STOCK_COSTS)
                        m = result.metrics()
                        if m["years"] < 1:
                            continue
                        ctrl_w = sh._build_random_weights(train_panel, train_panel.index[0],
                                                        train_end, k, step, seed=42)
                        ctrl_result = run_weights(train_panel, ctrl_w, STOCK_COSTS)
                        cm = ctrl_result.metrics()
                        records.append({
                            "config": f"mom{mlb}_gap{gw}",
                            "k": k, "step": step,
                            "cagr_pct": m["cagr_pct"], "sharpe": m["sharpe"],
                            "weekly_mean_pct": m["weekly_mean_pct"],
                            "max_dd_pct": m["max_drawdown_pct"],
                            "excess_sharpe": round(m["sharpe"] - cm["sharpe"], 3),
                            "weekly_p_ge_5pct": sh._weekly_hit_rate(result, 0.05),
                        })
                    except Exception:
                        pass

    records.sort(key=lambda r: r["weekly_mean_pct"], reverse=True)
    P(f"\n  {len(records)} valid configs")
    for r in records[:15]:
        P(f"  {r['config']}/k{r['k']}/step{r['step']}: "
          f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
          f"CAGR={r['cagr_pct']:.0f}% ex={r['excess_sharpe']:+.3f} "
          f"P(>=5%)={r['weekly_p_ge_5pct']}%")

    return records[:15]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    start = pd.Timestamp("2020-01-01")
    end = pd.Timestamp("2026-09-18")

    # Load NIFTY50+Midcap150 panel
    symbols = list(set(sh.parse_universe(ROOT / "data" / "universe" / "n50.txt") +
                      sh.parse_universe(ROOT / "data" / "universe" / "mid150.txt")))
    panel = build_panel_cached(symbols, start, end, min_bars=250)

    # OHLC for combined signal
    P("\nLoading OHLC data...")
    ohlc = sh._load_ohlc_for_panel(panel.columns.tolist(), panel.index)
    P(f"  OHLC loaded: {len(ohlc)} components" if ohlc else "  OHLC failed")

    # 1. Top Phase 1 configs for OOS validation
    oos_configs = [
        {"name": "mom_10d", "fn": sh.signal_momentum, "kwargs": {"lookback": 10}, "k": 5, "step": 1, "sizing": "equal"},
        {"name": "mom_10d", "fn": sh.signal_momentum, "kwargs": {"lookback": 10}, "k": 3, "step": 1, "sizing": "equal"},
        {"name": "mom_5d", "fn": sh.signal_momentum, "kwargs": {"lookback": 5}, "k": 3, "step": 10, "sizing": "equal"},
        {"name": "mom_10d", "fn": sh.signal_momentum, "kwargs": {"lookback": 10}, "k": 3, "step": 10, "sizing": "equal"},
        {"name": "mom_5d", "fn": sh.signal_momentum, "kwargs": {"lookback": 5}, "k": 2, "step": 10, "sizing": "equal"},
        {"name": "mom_10d", "fn": sh.signal_momentum, "kwargs": {"lookback": 10}, "k": 2, "step": 10, "sizing": "equal"},
        {"name": "mom_vol_10d_20d", "fn": sh.signal_composite_mom_vol, "kwargs": {"mom_lookback": 10, "vol_window": 20}, "k": 3, "step": 10, "sizing": "equal"},
    ]

    # OOS period starts at Phase 1 midpoint (2024-05-15)
    oos_results = test_oos_top_configs(panel, oos_configs, pd.Timestamp("2024-05-15"))

    # 2. Small-cap sweep
    sc_symbols = sh.parse_universe(ROOT / "data" / "universe" / "smallcap250.txt")
    P(f"\nLoading small-cap panel ({len(sc_symbols)} symbols)...")
    sc_panel = build_panel_cached(sc_symbols, pd.Timestamp("2020-01-01"), end, min_bars=250)
    sc_results = smallcap_sweep(sc_panel)

    # 3. Combined signal sweep
    combo_results = run_combined_sweep(panel, ohlc)

    # Save
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "oos_validation": oos_results,
        "smallcap_results": sc_results,
        "combined_signal_results": combo_results,
    }
    out = OUT / "hunt_fast.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    P(f"\n{'='*72}")
    P(f"Results saved to {out}")
    P(f"Done!")


if __name__ == "__main__":
    raise SystemExit(main())
