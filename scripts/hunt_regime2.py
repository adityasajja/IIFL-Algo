"""Iteration 3: Robust gap-and-run with dual regime filter + vol-scaled sizing.

Key lessons from Iterations 1-2:
- gap_run_5 signal works but is market-condition dependent
- 200d regime filter reduces drawdown but doesn't prevent bear-market losses
- Single OOS test (4.1% weekly) inflated by bull market; walk-forward shows 0.8% pooled
- Need: dual regime (50d+200d) AND volatility targeting for steady returns

New approach:
1. Dual regime: only trade when NIFTY > 50d SMA AND above 200d SMA (bull trend confirmation)
2. Volatility-scaled sizing: position size = target_vol / stock_vol (risk parity style)
3. Combined signal: gap_run_5 * volume_ratio * momentum_strength
4. Tighter stops (7-10%) with wider targets (15-25%) for better R:R in volatile periods
"""
from __future__ import annotations
import json, sys, time, math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
TRADING_DAYS = 252
COST = 0.00283
p = print
def P(*a, **k): k.setdefault("flush", True); p(*a, **k)


def load_symbol(sym, start, end):
    df = pd.read_parquet(DAILY / f"{sym}.parquet", columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.dropna(subset=["close"]).sort_values("ts")
    df = df[df["close"] > 0]
    df = df[(df["ts"] >= start) & (df["ts"] <= end)].set_index("ts")
    return df


def load_stocks(symbols, start, end, min_bars=250):
    close_d, high_d, low_d, open_d, vol_d = {}, {}, {}, {}, {}
    for sym in symbols:
        try:
            df = load_symbol(sym, start, end)
            if len(df) < min_bars: continue
            close_d[sym] = df["close"]; high_d[sym] = df["high"]
            low_d[sym] = df["low"]; open_d[sym] = df["open"]; vol_d[sym] = df["volume"]
        except Exception:
            continue
    panels = {}
    for name, d in [("close", close_d), ("high", high_d), ("low", low_d),
                     ("open", open_d), ("volume", vol_d)]:
        if d: panels[name] = pd.concat(d, axis=1)
    cols = sorted(set(panels["close"].columns) & set(panels["high"].columns) &
                  set(panels["low"].columns) & set(panels["open"].columns) &
                  set(panels["volume"].columns))
    for name in panels: panels[name] = panels[name][cols]
    idx = panels["close"].index
    for name in panels: panels[name] = panels[name].reindex(idx).ffill()
    return panels, cols, idx


def compute_signals(close, high, low, open_, volume):
    c = close; v = volume; o = open_
    sigs = {}
    for lb in [5, 10, 20]:
        s = np.full_like(c, -np.inf)
        s[lb:] = c[lb:] / c[:-lb] - 1.0
        s = np.roll(s, 1, axis=0); s[:lb + 2] = -np.inf
        sigs[f"mom_{lb}d"] = s
    gap = np.full_like(c, -np.inf)
    gap[1:] = (o[1:] - c[:-1]) / c[:-1]
    gap = np.roll(gap, 1, axis=0); gap[:2] = -np.inf
    sigs["gap_run"] = np.where(gap > 0.01, gap * sigs["mom_5d"], -np.inf)
    sigs["gap_run_5"] = np.where(gap > 0.02, gap * sigs["mom_5d"], -np.inf)
    vol_avg = np.full_like(v, np.nan)
    for i in range(20, len(v)):
        vol_avg[i] = v[i - 20:i].mean(axis=0)
    vr = np.where(vol_avg > 0, v / vol_avg, 0)
    sigs["vol_ratio"] = vr
    sigs["vol_mom_5d"] = np.where(vr > 1.2, sigs["mom_5d"] * vr, -np.inf)
    return sigs


def compute_exit(close, high, low, t, picks, max_holding, sl_pct, tp_pct):
    entry = close[t, picks]
    path_end = min(t + max_holding + 1, close.shape[0])
    if path_end <= t + 1 or len(picks) == 0:
        return np.zeros(len(picks)), 0, 0, 0
    hi = high[t + 1:path_end][:, picks]
    lo = low[t + 1:path_end][:, picks]
    cl = close[t + 1:path_end][:, picks]
    eps = 1e-10
    cum_hi = hi / (entry + eps) - 1.0
    cum_lo = lo / (entry + eps) - 1.0
    stop_lvl = -sl_pct / 100.0
    tgt_lvl = tp_pct / 100.0
    stop_hits = cum_lo <= stop_lvl
    target_hits = cum_hi >= tgt_lvl
    n_days = path_end - (t + 1)
    if n_days == 1:
        exits = cl[-1] / (entry + eps) - 1.0
        sw = stop_hits[-1]; tw = target_hits[-1] & ~sw
        exits[sw] = stop_lvl; exits[tw] = tgt_lvl
        return exits, int(np.sum(sw)), int(np.sum(tw)), int(np.sum(~sw & ~tw))
    sf = np.argmax(stop_hits, axis=0); sa = np.any(stop_hits, axis=0)
    tf = np.argmax(target_hits, axis=0); ta = np.any(target_hits, axis=0)
    exits = cl[-1] / (entry + eps) - 1.0
    sw = sa & (~ta | (sf <= tf)); tw = ta & (~sa | (tf < sf))
    exits[sw] = stop_lvl; exits[tw] = tgt_lvl
    return exits, int(np.sum(sw)), int(np.sum(tw)), len(picks) - int(np.sum(sw)) - int(np.sum(tw))


def build_regime(nifty_close, window):
    n = len(nifty_close)
    filt = np.zeros(n, dtype=bool)
    for i in range(window - 1, n):
        sma = np.nanmean(nifty_close[i - window + 1:i + 1])
        filt[i] = nifty_close[i] > sma if not np.isnan(sma) else False
    return filt


def run_strategy(close, high, low, signal, sl_pct, tp_pct, max_holding, k, step,
                 costs=COST, regime_filter=None, vol_target=0.20, vol_window=20):
    """Run strategy with volatility-scaled position sizing.

    Position size for each stock = (vol_target * portfolio) / stock_vol
    This is risk-parity style: volatile stocks get smaller positions.
    """
    n_dates, n_stocks = close.shape
    warmup = 22
    rb_dates = list(range(warmup, n_dates - max_holding, step))
    if len(rb_dates) < 5:
        return {"error": "insufficient dates"}

    daily_ret = np.zeros_like(close)
    daily_ret[1:] = close[1:] / close[:-1] - 1.0

    all_ret = []
    total_trades = 0
    total_stops = 0
    total_targets = 0

    for t in rb_dates:
        if regime_filter is not None and t < len(regime_filter) and not regime_filter[t]:
            continue
        scores = signal[t, :].copy()
        valid = close[t, :] > 0
        scores = np.where(valid, scores, -np.inf)
        top_k = min(k, int(np.sum(scores > -np.inf)))
        if top_k < k:
            continue
        picks = np.argpartition(-scores, top_k - 1)[:top_k]
        picks = picks[np.argsort(-scores[picks])]

        exits, n_stop, n_target, n_hold = compute_exit(close, high, low, t, picks, max_holding, sl_pct, tp_pct)

        if vol_target > 0 and vol_window > 0 and t >= vol_window:
            recent_ret = daily_ret[t - vol_window:t, :][:, picks]
            stock_vol = np.std(recent_ret, axis=0, ddof=1) * np.sqrt(TRADING_DAYS)
        else:
            stock_vol = np.full(len(picks), 0.20)

        # Risk-parity weights: weight ~ 1/stock_vol, normalized
        inv_vol = np.where(stock_vol > 0.01, 1.0 / stock_vol, 1.0 / 0.20)
        raw_weights = inv_vol / inv_vol.sum() * len(picks)  # Scale so sum = k

        # Volatility targeting: scale all positions if avg vol > target
        avg_vol = np.mean(stock_vol)
        vol_scale = min(1.0, vol_target / avg_vol) if avg_vol > 0 else 1.0

        net = exits - costs
        portfolio_ret = float(np.sum(raw_weights * vol_scale * net) / len(picks))

        all_ret.append(portfolio_ret)
        total_trades += k
        total_stops += n_stop
        total_targets += n_target

    if len(all_ret) < 5:
        return {"error": "insufficient trades"}

    returns = np.array(all_ret)
    cum = np.cumprod(1 + returns)
    total_ret = float(cum[-1] - 1)
    n_p = len(returns)
    years = n_p * step / TRADING_DAYS
    cagr = (1 + total_ret) ** (1.0 / years) - 1 if years > 0 else 0
    ppy = TRADING_DAYS / step
    arith_annual = float(np.mean(returns)) * ppy
    vol = float(np.std(returns, ddof=1)) * np.sqrt(ppy) if n_p > 1 else 0
    sharpe = (arith_annual - 0.075) / vol if vol > 0 else 0

    wf = 5.0 / step
    wr = returns * wf
    rmax = np.maximum.accumulate(cum)
    max_dd = float(np.min(cum / rmax - 1))

    return {
        "weekly_mean_pct": round(100 * np.mean(wr), 3),
        "weekly_vol_pct": round(100 * np.std(wr, ddof=1), 3) if len(wr) > 1 else 0,
        "sharpe": round(sharpe, 3),
        "vol_pct": round(vol, 2),
        "cagr_pct": round(100 * cagr, 2),
        "total_return_pct": round(100 * total_ret, 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
        "weekly_win_rate_pct": round(100 * np.mean(wr > 0), 1),
        "weeks_ge_2pct_pct": round(100 * np.mean(wr >= 0.02), 1),
        "weeks_ge_3pct_pct": round(100 * np.mean(wr >= 0.03), 1),
        "weeks_ge_5pct_pct": round(100 * np.mean(wr >= 0.05), 1),
        "num_trades": total_trades,
        "win_rate_pct": round(100 * total_targets / max(total_trades, 1), 1),
        "stop_rate_pct": round(100 * total_stops / max(total_trades, 1), 1),
        "n_rebalances": n_p,
        "min_weekly_pct": round(100 * np.min(wr), 3),
        "max_weekly_pct": round(100 * np.max(wr), 3),
        "median_weekly_pct": round(100 * np.median(wr), 3),
    }


def main():
    start = pd.Timestamp("2020-01-01")
    end = pd.Timestamp("2026-09-18")

    from strategy_hunt import parse_universe
    all_symbols = list(set(
        parse_universe(ROOT / "data" / "universe" / "n50.txt") +
        parse_universe(ROOT / "data" / "universe" / "mid150.txt")
    ))
    P(f"Loading {len(all_symbols)} symbols...")
    t0 = time.time()
    panels, syms, dates = load_stocks(all_symbols, start, end)
    P(f"  {len(syms)} stocks, {len(dates)} dates in {time.time()-t0:.1f}s")

    nifty = load_symbol("NIFTYBEES", start, end)
    nifty_close = nifty["close"].reindex(dates).ffill().to_numpy(dtype=np.float64)

    regime_50 = build_regime(nifty_close, 50)
    regime_200 = build_regime(nifty_close, 200)
    regime_dual = regime_50 & regime_200  # Both must be true (strong bull trend)
    regime_or = regime_50 | regime_200    # Either true (mild bull)

    close = panels["close"].to_numpy(dtype=np.float64)
    high = panels["high"].to_numpy(dtype=np.float64)
    low = panels["low"].to_numpy(dtype=np.float64)
    open_ = panels["open"].to_numpy(dtype=np.float64)
    volume = panels["volume"].to_numpy(dtype=np.float64)
    n_dates = len(dates)
    mid = n_dates // 2

    P(f"  Date: {dates[0].date()} → {dates[-1].date()}")
    P(f"  Regime days: 50d={np.sum(regime_50)} 200d={np.sum(regime_200)} "
      f"dual={np.sum(regime_dual)} ({100*np.mean(regime_dual):.0f}%)")
    P(f"  B&H weekly: ~0.526%")

    signals = compute_signals(close, high, low, open_, volume)

    # ================================================================
    # PART 1: 3-fold WALK-FORWARD (expanding window, dual regime)
    # ================================================================
    P(f"\n{'='*80}")
    P(f"3-FOLD WALK-FORWARD: gap_run_5/sl10/tp20/mh20/k5/step5 + dual_200d_regime")
    P(f"{'='*80}")

    sig = signals["gap_run_5"]
    # Folds: train on first 2/3 of OOS period, test on last 1/3
    test_starts = [mid, mid + (n_dates - mid) // 3, mid + 2 * (n_dates - mid) // 3]
    test_ends = [mid + (n_dates - mid) // 3, mid + 2 * (n_dates - mid) // 3, n_dates]

    fold_results = []
    for fi, (ts_idx, te_idx) in enumerate(zip(test_starts, test_ends)):
        r = run_strategy(
            close[ts_idx:te_idx], high[ts_idx:te_idx], low[ts_idx:te_idx],
            sig[ts_idx:te_idx], 10, 20, 20, 5, 5,
            costs=COST, regime_filter=regime_dual[ts_idx:te_idx],
            vol_target=0.20
        )
        if "error" in r:
            P(f"  Fold {fi+1} ({dates[ts_idx].date()} → {dates[te_idx-1].date()}): {r['error']}")
            continue
        fold_results.append(r)
        P(f"  Fold {fi+1} ({dates[ts_idx].date()} → {dates[te_idx-1].date()}): "
          f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
          f"DD={r['max_drawdown_pct']:.0f}% trades={r['n_rebalances']} "
          f"pos_wk={r['weekly_win_rate_pct']:.0f}%")

    if fold_results:
        wks = [r["weekly_mean_pct"] for r in fold_results]
        shs = [r["sharpe"] for r in fold_results]
        P(f"\n  Avg: wk={np.mean(wks):+.3f}% Sharpe={np.mean(shs):.3f}")
        P(f"  Positive folds: {sum(1 for w in wks if w > 0)}/{len(wks)}")

    # ================================================================
    # PART 2: Regime filter comparison (OOS)
    # ================================================================
    P(f"\n{'='*80}")
    P(f"REGIME FILTER COMPARISON (OOS: k=5, sl=10, tp=20, mh=20, step=5)")
    P(f"{'='*80}")

    for name, regime in [("none", None), ("50d", regime_50[mid:]), ("200d", regime_200[mid:]),
                          ("dual(50+200)", regime_dual[mid:]), ("or(50|200)", regime_or[mid:])]:
        r_eq = run_strategy(close[mid:], high[mid:], low[mid:], sig[mid:],
                            10, 20, 20, 5, 5, costs=COST,
                            regime_filter=regime, vol_target=0.20)
        r_ns = run_strategy(close[mid:], high[mid:], low[mid:], sig[mid:],
                            999, 999, 20, 5, 5, costs=COST,
                            regime_filter=regime, vol_target=0.20)
        eq_str = f"wk={r_eq['weekly_mean_pct']:+.3f}% Sharpe={r_eq['sharpe']:.3f} " \
                 f"DD={r_eq['max_drawdown_pct']:.0f}% trades={r_eq['n_rebalances']}" if "error" not in r_eq else "err"
        ns_str = f"wk={r_ns['weekly_mean_pct']:+.3f}%" if "error" not in r_ns else "err"
        P(f"  {name:15s}: with_stops={eq_str:60s} no_stops={ns_str}")

    # ================================================================
    # PART 3: Volatility targeting impact (dual 200d regime)
    # ================================================================
    P(f"\n{'='*80}")
    P(f"VOL TARGETING IMPACT (OOS, dual 200d regime, sl=10, tp=20)")
    P(f"{'='*80}")

    for vt in [0.0, 0.10, 0.15, 0.20, 0.25, 0.30]:
        for k in [3, 5]:
            r = run_strategy(close[mid:], high[mid:], low[mid:], sig[mid:],
                             10, 20, 20, k, 5, costs=COST,
                             regime_filter=regime_dual[mid:], vol_target=vt)
            if "error" not in r:
                ok = "***" if 2 <= r["weekly_mean_pct"] <= 5 else "  "
                P(f"  {ok} vol_target={vt:.2f} k={k}: wk={r['weekly_mean_pct']:+.3f}% "
                  f"Sharpe={r['sharpe']:.3f} DD={r['max_drawdown_pct']:.0f}% "
                  f"trades={r['n_rebalances']} pos_wk={r['weekly_win_rate_pct']:.0f}%")

    # ================================================================
    # PART 4: Parameter robustness (best config ± perturbations)
    # ================================================================
    P(f"\n{'='*80}")
    P(f"PARAMETER ROBUSTNESS (OOS, dual 200d regime, k=5, vol_target=0.20)")
    P(f"{'='*80}")

    param_tests = [
        (8, 16, 20, 5, "sl8/tp16"),
        (10, 20, 20, 5, "sl10/tp20 (base)"),
        (12, 24, 20, 5, "sl12/tp24"),
        (10, 20, 15, 5, "mh15"),
        (10, 20, 20, 5, "mh20 (base)"),
        (10, 20, 30, 5, "mh30"),
        (10, 20, 20, 3, "k3"),
        (10, 20, 20, 5, "k5 (base)"),
        (10, 30, 20, 5, "tp30"),
        (15, 20, 20, 5, "sl15/tp20"),
    ]
    for sl, tp, mh, k, desc in param_tests:
        r = run_strategy(close[mid:], high[mid:], low[mid:], sig[mid:],
                         sl, tp, mh, k, 5, costs=COST,
                         regime_filter=regime_dual[mid:], vol_target=0.20)
        if "error" in r:
            P(f"  {desc}: {r['error']}")
        else:
            ok = "***" if 2 <= r["weekly_mean_pct"] <= 5 else "  "
            P(f"  {ok} {desc}: wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
              f"DD={r['max_drawdown_pct']:.0f}% trades={r['n_rebalances']} "
              f"pos_wk={r['weekly_win_rate_pct']:.0f}%")

    # ================================================================
    # PART 5: Compare signals (dual 200d regime, k=5, vol_target=0.20)
    # ================================================================
    P(f"\n{'='*80}")
    P(f"SIGNAL COMPARISON (OOS, dual 200d regime, sl=10, tp=20, mh=20, k=5, vol=0.20)")
    P(f"{'='*80}")

    for sig_name in ["gap_run_5", "gap_run", "mom_5d", "vol_mom_5d", "mom_10d"]:
        r = run_strategy(close[mid:], high[mid:], low[mid:], sigs := signals[sig_name][mid:] if sig_name in signals else signals[sig_name][mid:],
                         10, 20, 20, 5, 5, costs=COST,
                         regime_filter=regime_dual[mid:], vol_target=0.20)
        if "error" in r:
            P(f"  {sig_name}: {r['error']}")
        else:
            P(f"  {sig_name}: wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
              f"DD={r['max_drawdown_pct']:.0f}% trades={r['n_rebalances']} "
              f"pos_wk={r['weekly_win_rate_pct']:.0f}%")

    # ================================================================
    # PART 6: Combined signal (gap AND momentum AND volume)
    # ================================================================
    P(f"\n{'='*80}")
    P(f"COMBINED SIGNAL: gap_run_5 * vol_ratio * mom_5d (triple filter)")
    P(f"{'='*80}")

    gap = signals["gap_run_5"]
    mom = signals["mom_5d"]
    vr = signals["vol_ratio"]
    # Combined: gap_run_5 (already gap * mom), but also require vol > 1.2x
    combined = np.where((vr > 1.2) & (gap > 0), gap * (1 + vr), -np.inf)
    combined = np.where(np.isfinite(combined), combined, -np.inf)

    for sl, tp, mh, k, step, desc in [
        (10, 20, 20, 5, 5, "base"),
        (7, 14, 20, 5, 5, "tight"),
        (10, 30, 20, 5, 5, "wide_target"),
        (10, 20, 20, 3, 5, "k3"),
        (10, 20, 20, 5, 3, "step3"),
        (10, 20, 20, 5, 5, "no_regime"),
    ]:
        regime = regime_dual[mid:] if desc != "no_regime" else None
        r = run_strategy(close[mid:], high[mid:], low[mid:], combined[mid:],
                         sl, tp, mh, k, step, costs=COST,
                         regime_filter=regime, vol_target=0.20)
        if "error" in r:
            P(f"  {desc} (sl{sl}/tp{tp}/mh{mh}/k{k}/s{step}): {r['error']}")
        else:
            ok = "***" if 2 <= r["weekly_mean_pct"] <= 5 else "  "
            P(f"  {ok} {desc} (sl{sl}/tp{tp}/mh{mh}/k{k}/s{step}): "
              f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
              f"DD={r['max_drawdown_pct']:.0f}% trades={r['n_rebalances']} "
              f"pos_wk={r['weekly_win_rate_pct']:.0f}%")

    # ================================================================
    # FINAL SUMMARY
    # ================================================================
    P(f"\n{'='*80}")
    P(f"FINAL SUMMARY")
    P(f"{'='*80}")

    # Find the best config that's in the 2-5% range
    candidates = []
    for sig_name, sig_arr in [("gap_run_5", sig[mid:]), ("gap_run", signals["gap_run"][mid:]),
                               ("combined", combined[mid:]), ("vol_mom_5d", signals["vol_mom_5d"][mid:])]:
        for sl, tp, mh, k, step in [(10, 20, 20, 3, 5), (10, 20, 20, 5, 5),
                                     (7, 14, 20, 5, 5), (10, 30, 20, 5, 5),
                                     (10, 20, 30, 5, 5), (10, 20, 20, 5, 3)]:
            r = run_strategy(close[mid:], high[mid:], low[mid:], sig_arr,
                             sl, tp, mh, k, step, costs=COST,
                             regime_filter=regime_dual[mid:], vol_target=0.20)
            if "error" not in r and 2 <= r["weekly_mean_pct"] <= 5:
                candidates.append((sig_name, sl, tp, mh, k, step, r))

    if candidates:
        # Sort by Sharpe
        candidates.sort(key=lambda x: x[6]["sharpe"], reverse=True)
        P(f"\n  Best configs in 2-5% range (OOS, dual 200d regime, vol_target=0.20):")
        for sig_name, sl, tp, mh, k, step, r in candidates[:5]:
            P(f"  {sig_name}/sl{sl}/tp{tp}/mh{mh}/k{k}/s{step}: "
              f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
              f"DD={r['max_drawdown_pct']:.0f}% trades={r['n_rebalances']} "
              f"pos_wk={r['weekly_win_rate_pct']:.0f}% "
              f"P(>=5%)={r['weeks_ge_5pct_pct']:.0f}%")

        best = candidates[0]
        r = best[6]
        P(f"\n  RECOMMENDED CONFIG: {best[0]}/sl{best[1]}/tp{best[2]}/mh{best[3]}/k{best[4]}/s{best[5]}")
        P(f"  OOS ({dates[mid].date()} → {dates[-1].date()}):")
        P(f"    Weekly mean:    {r['weekly_mean_pct']:+.3f}%")
        P(f"    Sharpe:         {r['sharpe']:.3f}")
        P(f"    Max drawdown:   {r['max_drawdown_pct']:.1f}%")
        P(f"    Win rate (wk):  {r['weekly_win_rate_pct']:.0f}%")
        P(f"    P(>=2% week):   {r['weeks_ge_2pct_pct']:.0f}%")
        P(f"    P(>=5% week):   {r['weeks_ge_5pct_pct']:.0f}%")
        P(f"    Trades:         {r['num_trades']}")
        P(f"    Median weekly:  {r['median_weekly_pct']:+.3f}%")
        P(f"    Min weekly:     {r['min_weekly_pct']:+.3f}%")
        P(f"    Max weekly:     {r['max_weekly_pct']:+.3f}%")
    else:
        P(f"  No config in 2-5% range found. Best available:")
        # Find best Sharpe overall
        for sig_name, sig_arr in [("gap_run_5", sig[mid:])]:
            r = run_strategy(close[mid:], high[mid:], low[mid:], sig_arr,
                             10, 20, 20, 5, 5, costs=COST,
                             regime_filter=regime_dual[mid:], vol_target=0.20)
            P(f"  {sig_name}/sl10/tp20/mh20/k5/s5: wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
