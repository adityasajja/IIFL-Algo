"""Targeted follow-up: test Phase 1 winners out-of-sample and explore
small-cap + combined-signal configurations that push toward 2-5%/week.

Key hypotheses from Phase 1:
  - mom_10d with step=1 (daily) gives ~2% weekly mean but high cost drag
  - mom_5d/mom_10d with step=10, k=3 gives Sharpe 3.0+ with 1.6-1.7% weekly mean
  - Small-cap stocks are more volatile → higher potential weekly returns
  - Combined signals (momentum + gap + volume) may boost conviction

This script:
  1. Validates top Phase 1 configs on OOS period (2024-05-15 → 2026-09-18)
  2. Runs small-cap sweep with k=2,3 and daily/weekly rebalancing
  3. Tests combined momentum+gap signal
"""
from __future__ import annotations
import json, sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import strategy_hunt as sh
from atr.research.hunt import (
    STOCK_COSTS, ETF_COSTS, Result, TRADING_DAYS,
    buy_and_hold, run_weights, deflated_sharpe,
)

DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
OUT_DIR = ROOT / "data" / "research"

# ---------------------------------------------------------------------------
# Top configs from Phase 1 (train period results)
# ---------------------------------------------------------------------------
TOP_CONFIGS = [
    # (signal_name, signal_fn, kwargs, k, step, sizing)
    ("mom_10d", sh.signal_momentum, {"lookback": 10}, 5, 1, "equal"),
    ("mom_10d", sh.signal_momentum, {"lookback": 10}, 3, 1, "equal"),
    ("mom_5d", sh.signal_momentum, {"lookback": 5}, 3, 10, "equal"),
    ("mom_10d", sh.signal_momentum, {"lookback": 10}, 3, 10, "equal"),
    ("mom_5d", sh.signal_momentum, {"lookback": 5}, 2, 10, "equal"),
    ("mom_10d", sh.signal_momentum, {"lookback": 10}, 2, 10, "equal"),
    ("mom_vol_10d_20d", sh.signal_composite_mom_vol, {"mom_lookback": 10, "vol_window": 20}, 3, 10, "equal"),
]


def load_ohlc_cached(symbols, index):
    """Load OHLCV for symbols, cached per call (called once per universe)."""
    return sh._load_ohlc_for_panel(symbols, index)


def test_config_oos(panel, sig_fn, kwargs, k, step, sizing, start, end, costs):
    """Test a single config on a specific panel period. Returns metrics dict."""
    sig = sig_fn(panel, **kwargs) if not _needs_ohlc(sig_fn) else None
    invvol = 1.0 / panel.pct_change().rolling(63, min_periods=40).std()
    invvol = invvol.replace([np.inf, -np.inf], np.nan)
    weights = sh._build_weights(panel, sig, invvol, panel.index[0],
                                panel.index[-1], k, step, sizing)
    if weights.empty:
        return {"error": "empty weights"}
    result = run_weights(panel, weights, costs)
    m = result.metrics()
    if m["years"] < 1:
        return {"error": f"too few years: {m['years']}"}
    return sh._slim_metrics(m)


def validate_top_configs(symbols, start, end):
    """Validate Phase 1 top configs on OOS period."""
    print(f"\n{'='*72}")
    print(f"VALIDATING TOP CONFIGS ON OOS PERIOD")
    print(f"{'='*72}")
    print(f"Universe: {len(symbols)} stocks")
    print(f"Window: {start.date()} → {end.date()}")

    panel = sh.build_universe_panel(symbols, start, end, min_bars=250)
    if panel.empty:
        print("  No data!")
        return {}

    mid = len(panel) // 2
    oos_start = panel.index[mid]
    oos_panel = panel.loc[panel.index >= oos_start]
    train_panel = panel.loc[panel.index < oos_start]

    print(f"  Train: {train_panel.index[0].date()} → {train_panel.index[-1].date()} ({len(train_panel)} bars)")
    print(f"  OOS:   {oos_panel.index[0].date()} → {oos_panel.index[-1].date()} ({len(oos_panel)} bars)")

    # Benchmark on OOS
    bh_w = sh._equal_weight_hold(oos_panel, oos_panel.index[0])
    bh = run_weights(oos_panel, bh_w, STOCK_COSTS)
    bh_m = bh.metrics()
    print(f"\n  B&H (OOS): CAGR={bh_m['cagr_pct']:.2f}% Sharpe={bh_m['sharpe']:.3f} "
          f"weekly={bh_m['weekly_mean_pct']:+.4f}% DD={bh_m['max_drawdown_pct']:.1f}%")

    results = []
    for name, fn, kw, k, step, sizing in TOP_CONFIGS:
        try:
            # Train metrics
            train_sig = fn(train_panel, **kw)
            train_invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
            train_invvol = train_invvol.replace([np.inf, -np.inf], np.nan)
            train_w = sh._build_weights(train_panel, train_sig, train_invvol,
                                        train_panel.index[0], train_panel.index[-1],
                                        k, step, sizing)
            train_result = run_weights(train_panel, train_w, STOCK_COSTS)
            train_m = train_result.metrics()

            # OOS metrics
            oos_sig = fn(oos_panel, **kw)
            oos_invvol = 1.0 / oos_panel.pct_change().rolling(63, min_periods=40).std()
            oos_invvol = oos_invvol.replace([np.inf, -np.inf], np.nan)
            oos_w = sh._build_weights(oos_panel, oos_sig, oos_invvol,
                                        oos_panel.index[0], oos_panel.index[-1],
                                        k, step, sizing)
            if oos_w.empty:
                continue
            oos_result = run_weights(oos_panel, oos_w, STOCK_COSTS)
            oos_m = oos_result.metrics()

            print(f"\n  {name}/k{k}/step{step}/{sizing}:")
            print(f"    Train: CAGR={train_m['cagr_pct']:.1f}% Sharpe={train_m['sharpe']:.3f} "
                  f"weekly={train_m['weekly_mean_pct']:+.3f}% DD={train_m['max_drawdown_pct']:.1f}%")
            print(f"    OOS:   CAGR={oos_m['cagr_pct']:.1f}% Sharpe={oos_m['sharpe']:.3f} "
                  f"weekly={oos_m['weekly_mean_pct']:+.3f}% DD={oos_m['max_drawdown_pct']:.1f}%")
            print(f"    P(>=5%/wk)={sh._weekly_hit_rate(oos_result, 0.05)}% "
                  f"P(>=2%/wk)={oos_m['weeks_ge_2pct_pct']}% "
                  f"cost_drag={oos_m['cost_drag_pct_yr']:.2f}%/yr")

            results.append({
                "signal": name, "k": k, "step": step, "sizing": sizing,
                "train": sh._slim_metrics(train_m),
                "oos": sh._slim_metrics(oos_m),
                "oos_p_ge_5pct": sh._weekly_hit_rate(oos_result, 0.05),
                "decay": round(oos_m["weekly_mean_pct"] - train_m["weekly_mean_pct"], 4),
            })
        except Exception as e:
            print(f"  {name}/k{k}/step{step}/{sizing}: ERROR: {e}")

    return {"benchmark": sh._slim_metrics(bh_m), "results": results}


# ---------------------------------------------------------------------------
# Small-cap sweep
# ---------------------------------------------------------------------------
def smallcap_sweep(start, end):
    """Sweep short-horizon signals on small-cap stocks (higher volatility)."""
    print(f"\n{'='*72}")
    print(f"SMALL-CAP SWEEP (higher volatility → higher potential returns)")
    print(f"{'='*72}")

    symbols = sh.parse_universe(ROOT / "data" / "universe" / "smallcap250.txt")
    print(f"  Universe: {len(symbols)} small-cap symbols")

    panel = sh.build_universe_panel(symbols, start, end, min_bars=250)
    if panel.empty:
        print("  No data!")
        return {}
    print(f"  Panel: {panel.shape[1]} stocks, {panel.shape[0]} bars")

    mid = len(panel) // 2
    train_panel = panel.iloc[:mid + 252]
    train_end = train_panel.index[-1]
    invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
    invvol = invvol.replace([np.inf, -np.inf], np.nan)

    # Benchmark
    bh_w = sh._equal_weight_hold(train_panel, train_panel.index[0])
    bh = run_weights(train_panel, bh_w, STOCK_COSTS)
    bh_m = bh.metrics()
    print(f"\n  B&H: CAGR={bh_m['cagr_pct']:.2f}% Sharpe={bh_m['sharpe']:.3f} "
          f"weekly={bh_m['weekly_mean_pct']:+.3f}% DD={bh_m['max_drawdown_pct']:.1f}%")

    signals_to_test = [
        ("mom_1d", sh.signal_momentum, {"lookback": 1}),
        ("mom_2d", sh.signal_momentum, {"lookback": 2}),
        ("mom_3d", sh.signal_momentum, {"lookback": 3}),
        ("mom_5d", sh.signal_momentum, {"lookback": 5}),
        ("mom_10d", sh.signal_momentum, {"lookback": 10}),
        ("mom_5d_vol", sh.signal_composite_mom_vol, {"mom_lookback": 5, "vol_window": 20}),
        ("mom_3d_vol", sh.signal_composite_mom_vol, {"mom_lookback": 3, "vol_window": 20}),
        ("mom_2d_vol", sh.signal_composite_mom_vol, {"mom_lookback": 2, "vol_window": 20}),
        ("rev_3d_10d", sh.signal_mean_reversion, {"lookback": 3, "vol_window": 10}),
        ("rev_2d_10d", sh.signal_mean_reversion, {"lookback": 2, "vol_window": 10}),
    ]

    records = []
    for sig_name, fn, kw in signals_to_test:
        try:
            sig = fn(train_panel, **kw)
        except Exception:
            continue
        for k in [2, 3, 5]:
            for step in [1, 5, 10]:
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
                        rec = {
                            "signal": sig_name, "k": k, "step": step, "sizing": sizing,
                            "cagr_pct": m["cagr_pct"], "sharpe": m["sharpe"],
                            "weekly_mean_pct": m["weekly_mean_pct"],
                            "max_dd_pct": m["max_drawdown_pct"],
                            "turnover_per_yr": m["turnover_per_yr"],
                            "cost_drag_pct_yr": m["cost_drag_pct_yr"],
                            "weeks_ge_2pct_pct": m["weeks_ge_2pct_pct"],
                            "weekly_p_ge_5pct": sh._weekly_hit_rate(result, 0.05),
                            "excess_sharpe": round(m["sharpe"] - cm["sharpe"], 3),
                        }
                        records.append(rec)
                    except Exception:
                        pass

    records.sort(key=lambda r: r["weekly_mean_pct"], reverse=True)
    print(f"\n  {len(records)} valid configs")
    if records:
        best = records[0]
        print(f"  Best weekly mean: {best['signal']}/k{best['k']}/step{best['step']}/{best['sizing']}")
        print(f"    weekly={best['weekly_mean_pct']:.4f}% Sharpe={best['sharpe']:.3f} "
              f"CAGR={best['cagr_pct']:.1f}% DD={best['max_dd_pct']:.1f}%")
        for r in records[:10]:
            print(f"  {r['signal']}/k{r['k']}/step{r['step']}/{r['sizing']}: "
                  f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
                  f"CAGR={r['cagr_pct']:.0f}% ex_sharpe={r['excess_sharpe']:+.3f} "
                  f"P(>=5%)={r['weekly_p_ge_5pct']}%")

    return {"benchmark": sh._slim_metrics(bh_m), "top_15": records[:15],
            "max_weekly_mean": max((r["weekly_mean_pct"] for r in records), default=None)}


# ---------------------------------------------------------------------------
# Combined signal: momentum + gap + volume
# ---------------------------------------------------------------------------
def combined_mom_gap_signal(panel, ohlc, mom_lookback, gap_lookback):
    """Combine momentum and gap signals for higher conviction ranking."""
    close = panel
    closes, opens, _, _, volumes = ohlc
    # Momentum signal
    mom = (close / close.shift(mom_lookback) - 1.0).shift(1)
    # Gap signal: recent gap up on volume surge
    gap = (opens / close.shift(1) - 1.0).shift(1)
    vol_ratio = volumes / volumes.shift(5).rolling(5, min_periods=3).mean()
    gap_score = gap * np.sqrt(vol_ratio.clip(upper=10))
    # Composite: rank both, then average ranks
    mom_rank = mom.rank(axis=1, ascending=False, method="average")
    gap_rank = gap_score.rank(axis=1, ascending=False, method="average")
    combined_rank = (mom_rank + gap_rank) / 2
    # Lower rank = better; convert to score
    score = -combined_rank
    return score


def combined_signal_sweep(symbols, start, end):
    """Test combined momentum+gap signal for higher conviction."""
    print(f"\n{'='*72}")
    print(f"COMBINED MOMENTUM+GAP SIGNAL SWEEP")
    print(f"{'='*72}")

    panel = sh.build_universe_panel(symbols, start, end, min_bars=250)
    if panel.empty:
        print("  No data!")
        return {}

    mid = len(panel) // 2
    train_panel = panel.iloc[:mid + 252]
    train_end = train_panel.index[-1]

    ohlc = load_ohlc_cached(train_panel.columns.tolist(), train_panel.index)
    if ohlc is None:
        print("  Failed to load OHLC")
        return {}

    invvol = 1.0 / train_panel.pct_change().rolling(63, min_periods=40).std()
    invvol = invvol.replace([np.inf, -np.inf], np.nan)

    bh_w = sh._equal_weight_hold(train_panel, train_panel.index[0])
    bh = run_weights(train_panel, bh_w, STOCK_COSTS)
    bh_m = bh.metrics()
    print(f"  B&H: CAGR={bh_m['cagr_pct']:.2f}% Sharpe={bh_m['sharpe']:.3f} "
          f"weekly={bh_m['weekly_mean_pct']:+.3f}%")

    records = []
    mom_lookbacks = [1, 2, 3, 5, 10]
    gap_lookbacks = [1, 2, 3, 5]
    for mlb in mom_lookbacks:
        for glb in gap_lookbacks:
            try:
                sig = combined_mom_gap_signal(train_panel, ohlc, mlb, glb)
                for k in [2, 3, 5]:
                    for step in [1, 5, 10]:
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
                                "config": f"mom{mlb}_gap{glb}",
                                "k": k, "step": step,
                                "cagr_pct": m["cagr_pct"], "sharpe": m["sharpe"],
                                "weekly_mean_pct": m["weekly_mean_pct"],
                                "max_dd_pct": m["max_drawdown_pct"],
                                "excess_sharpe": round(m["sharpe"] - cm["sharpe"], 3),
                                "weekly_p_ge_5pct": sh._weekly_hit_rate(result, 0.05),
                            })
                        except Exception:
                            pass
            except Exception as e:
                print(f"  mom{mlb}_gap{glb}: ERROR: {e}")

    records.sort(key=lambda r: r["weekly_mean_pct"], reverse=True)
    print(f"\n  {len(records)} valid configs")
    if records:
        for r in records[:15]:
            print(f"  {r['config']}/k{r['k']}/step{r['step']}: "
                  f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
                  f"CAGR={r['cagr_pct']:.0f}% ex_sharpe={r['excess_sharpe']:+.3f} "
                  f"P(>=5%)={r['weekly_p_ge_5pct']}%")

    return {"top_15": records[:15],
            "max_weekly_mean": max((r["weekly_mean_pct"] for r in records), default=None)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    start = pd.Timestamp("2015-01-01")
    end = pd.Timestamp("2026-09-18")

    # Use NIFTY50+Midcap150 symbols
    symbols = list(set(sh.parse_universe(ROOT / "data" / "universe" / "n50.txt") +
                      sh.parse_universe(ROOT / "data" / "universe" / "mid150.txt")))

    # 1. Validate top configs on OOS
    oos_results = validate_top_configs(symbols, start, end)

    # 2. Small-cap sweep
    sc_results = smallcap_sweep(start, end)

    # 3. Combined signal sweep
    combo_results = combined_signal_sweep(symbols, start, end)

    # Save all results
    payload = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "oos_validation": oos_results,
        "smallcap_sweep": sc_results,
        "combined_signal": combo_results,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "targeted_hunt.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\n{'='*72}")
    print(f"Results saved to {out}")

    # Summary
    max_oos_weekly = max((r["oos"]["weekly_mean_pct"] for r in oos_results.get("results", [])), default=None)
    max_sc_weekly = sc_results.get("max_weekly_mean")
    max_combo_weekly = combo_results.get("max_weekly_mean")
    print(f"\nMax weekly means found:")
    print(f"  OOS validation:  {max_oos_weekly}%")
    print(f"  Small-cap sweep: {max_sc_weekly}%")
    print(f"  Combined signal: {max_combo_weekly}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
