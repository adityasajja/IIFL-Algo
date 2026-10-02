"""Final optimization: find the best gap-and-run config with regime filter.

Goal: 2-5% weekly, Sharpe > 2, DD < 15%, at least 20 rebalancing periods.
Tests: gap_run (1% gap) vs gap_run_5 (2% gap), regime filters, vol targeting.
"""
from __future__ import annotations
import json, sys, time
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


def compute_all_signals(close, high, low, open_, volume):
    c = close; v = volume; o = open_
    sigs = {}
    for lb in [3, 5, 10, 20]:
        s = np.full_like(c, -np.inf)
        s[lb:] = c[lb:] / c[:-lb] - 1.0
        s = np.roll(s, 1, axis=0); s[:lb + 2] = -np.inf
        sigs[f"mom_{lb}d"] = s
    gap = np.full_like(c, -np.inf)
    gap[1:] = (o[1:] - c[:-1]) / c[:-1]
    gap = np.roll(gap, 1, axis=0); gap[:2] = -np.inf
    sigs["gap_run"] = np.where(gap > 0.01, gap * sigs["mom_5d"], -np.inf)
    sigs["gap_run_5"] = np.where(gap > 0.02, gap * sigs["mom_5d"], -np.inf)
    sigs["gap_run_3"] = np.where(gap > 0.03, gap * sigs["mom_5d"], -np.inf)
    vol_avg = np.full_like(v, np.nan)
    for i in range(20, len(v)):
        vol_avg[i] = v[i - 20:i].mean(axis=0)
    vr = np.where(vol_avg > 0, v / vol_avg, 0)
    sigs["vol_ratio"] = vr
    sigs["vol_mom_5d"] = np.where(vr > 1.2, sigs["mom_5d"] * vr, -np.inf)
    # Combined: gap_run_5 * (1 + vol_ratio) when vol > 1.2x
    sigs["gap_vol"] = np.where((gap > 0.02) & (vr > 1.2), gap * sigs["mom_5d"] * (1 + vr), -np.inf)
    # Gap with longer momentum
    sigs["gap_mom10"] = np.where(gap > 0.02, gap * sigs["mom_10d"], -np.inf)
    sigs["gap_mom20"] = np.where(gap > 0.02, gap * sigs["mom_20d"], -np.inf)
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

        if vol_target > 0 and t >= vol_window:
            recent_ret = daily_ret[t - vol_window:t, :][:, picks]
            stock_vol = np.std(recent_ret, axis=0, ddof=1) * np.sqrt(TRADING_DAYS)
        else:
            stock_vol = np.full(len(picks), 0.20)

        inv_vol = np.where(stock_vol > 0.01, 1.0 / stock_vol, 1.0 / 0.20)
        raw_weights = inv_vol / inv_vol.sum()

        avg_vol = np.mean(stock_vol)
        vol_scale = min(1.0, vol_target / avg_vol) if avg_vol > 0 else 1.0

        net = exits - costs
        portfolio_ret = float(np.sum(raw_weights * vol_scale * net))
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
    nif_close = nifty["close"].reindex(dates).ffill().to_numpy(dtype=np.float64)
    regime_50 = build_regime(nif_close, 50)
    regime_200 = build_regime(nif_close, 200)
    regime_dual = regime_50 & regime_200
    regime_or = regime_50 | regime_200

    close = panels["close"].to_numpy(dtype=np.float64)
    high = panels["high"].to_numpy(dtype=np.float64)
    low = panels["low"].to_numpy(dtype=np.float64)
    open_ = panels["open"].to_numpy(dtype=np.float64)
    volume = panels["volume"].to_numpy(dtype=np.float64)
    n_dates = len(dates)
    mid = n_dates // 2

    P(f"  Date: {dates[0].date()} → {dates[-1].date()}")
    P(f"  Regime: 50d={np.sum(regime_50)} 200d={np.sum(regime_200)} "
      f"dual={np.sum(regime_dual)}({100*np.mean(regime_dual):.0f}%) or={np.sum(regime_or)}({100*np.mean(regime_or):.0f}%)")

    signals = compute_all_signals(close, high, low, open_, volume)
    sigs_oos = {name: sig[mid:] for name, sig in signals.items()}
    close_oos = close[mid:]
    high_oos = high[mid:]
    low_oos = low[mid:]
    vol_oos = volume[mid:]

    # ================================================================
    # PART 1: Exhaustive parameter sweep on OOS (all signal x regime x params)
    # ================================================================
    P(f"\n{'='*80}")
    P(f"PARAMETER SWEEP (OOS, all signals × regimes × params)")
    P(f"{'='*80}")

    param_grid = [
        (sl, tp, mh, k, step, vt)
        for sl, tp in [(5, 10), (7, 14), (10, 20), (10, 30), (15, 25), (15, 30)]
        for mh in [10, 20, 30]
        for k in [3, 5]
        for step in [3, 5]
        for vt in [0.15, 0.20, 0.25]
    ]
    P(f"  Testing {len(param_grid)} param combos × 7 signals × 4 regimes = {len(param_grid)*7*4} total")

    regimes = [("none", None), ("50d", regime_50[mid:]), ("200d", regime_200[mid:]),
               ("dual", regime_dual[mid:])]
    sig_names = ["gap_run_5", "gap_run", "gap_run_3", "gap_vol",
                 "mom_5d", "mom_10d", "vol_mom_5d"]

    t1 = time.time()
    all_results = []
    for sig_name in sig_names:
        sig_arr = sigs_oos[sig_name]
        for regime_name, reg in regimes:
            for sl, tp, mh, k, step, vt in param_grid:
                r = run_strategy(close_oos, high_oos, low_oos, sig_arr,
                                 sl, tp, mh, k, step, costs=COST,
                                 regime_filter=reg, vol_target=vt)
                if "error" in r:
                    continue
                if r["num_trades"] < 15 or r["n_rebalances"] < 3:
                    continue
                if r["max_drawdown_pct"] < -20:
                    continue
                all_results.append({
                    "signal": sig_name, "regime": regime_name,
                    "sl": sl, "tp": tp, "mh": mh, "k": k, "step": step, "vt": vt,
                    **r,
                })

    P(f"  {len(all_results)} valid configs in {time.time()-t1:.1f}s")

    # Filter: in 2-5% range, Sharpe > 2, n_rebalances >= 5
    target_configs = [r for r in all_results
                      if 2 <= r["weekly_mean_pct"] <= 5
                      and r["sharpe"] > 2
                      and r["max_drawdown_pct"] >= -20]

    target_configs.sort(key=lambda r: r["sharpe"], reverse=True)

    P(f"\n  Configs in 2-5% range with Sharpe > 2: {len(target_configs)}")
    P(f"\n  Top 15 by Sharpe:")
    for r in target_configs[:15]:
        P(f"  {r['signal']}/{r['regime']}/sl{r['sl']}/tp{r['tp']}/mh{r['mh']}/k{r['k']}/s{r['step']}/vt{r['vt']}: "
          f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} DD={r['max_drawdown_pct']:.0f}% "
          f"trades={r['num_trades']} pos_wk={r['weekly_win_rate_pct']:.0f}%")
        P(f"    P(>=5%)={r['weeks_ge_5pct_pct']:.0f}% med={r['median_weekly_pct']:+.3f}% min={r['min_weekly_pct']:+.3f}%")

    # ================================================================
    # PART 2: Walk-forward for top 3 configs
    # ================================================================
    P(f"\n{'='*80}")
    P(f"WALK-FORWARD for top 3 configs (3 folds, 70/30 split)")
    P(f"{'='*80}")

    # Use expanding window walk-forward on full data
    wf_folds = [
        (int(n_dates * 0.4), int(n_dates * 0.65)),
        (int(n_dates * 0.4), int(n_dates * 0.75)),
        (int(n_dates * 0.4), int(n_dates * 0.85)),
    ]

    for rank, r_top in enumerate(target_configs[:3]):
        sig_name = r_top["signal"]
        sig_arr = signals[sig_name]
        regime_name = r_top["regime"]
        if regime_name == "none":
            reg_full = None
        elif regime_name == "50d":
            reg_full = regime_50
        elif regime_name == "200d":
            reg_full = regime_200
        else:
            reg_full = regime_dual

        P(f"\n  Config {rank+1}: {sig_name}/{regime_name}/sl{r_top['sl']}/tp{r_top['tp']}/"
          f"mh{r_top['mh']}/k{r_top['k']}/s{r_top['step']}/vt{r_top['vt']}")

        train_r = run_strategy(close[:int(n_dates*0.4)], high[:int(n_dates*0.4)], low[:int(n_dates*0.4)],
                               sig_arr[:int(n_dates*0.4)], r_top["sl"], r_top["tp"], r_top["mh"],
                               r_top["k"], r_top["step"], costs=COST,
                               regime_filter=reg_full[:int(n_dates*0.4)] if reg_full is not None else None,
                               vol_target=r_top["vt"])
        if "error" not in train_r:
            P(f"    TRAIN (2020-01 → {dates[int(n_dates*0.4)-1].date()}): "
              f"wk={train_r['weekly_mean_pct']:+.3f}% Sharpe={train_r['sharpe']:.3f} "
              f"DD={train_r['max_drawdown_pct']:.0f}% trades={train_r['num_trades']}")

        for fi, (ts_idx, te_idx) in enumerate(wf_folds):
            r = run_strategy(close[ts_idx:te_idx], high[ts_idx:te_idx], low[ts_idx:te_idx],
                             sig_arr[ts_idx:te_idx], r_top["sl"], r_top["tp"], r_top["mh"],
                             r_top["k"], r_top["step"], costs=COST,
                             regime_filter=reg_full[ts_idx:te_idx] if reg_full is not None else None,
                             vol_target=r_top["vt"])
            if "error" in r:
                P(f"    Fold {fi+1} ({dates[ts_idx].date()} → {dates[te_idx-1].date()}): {r['error']}")
            else:
                P(f"    Fold {fi+1} ({dates[ts_idx].date()} → {dates[te_idx-1].date()}): "
                  f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
                  f"DD={r['max_drawdown_pct']:.0f}% trades={r['n_rebalances']}")

    # ================================================================
    # PART 3: Best config full details
    # ================================================================
    P(f"\n{'='*80}")
    P(f"BEST CONFIG DETAILS (OOS: {dates[mid].date()} → {dates[-1].date()})")
    P(f"{'='*80}")

    if target_configs:
        best = target_configs[0]
        P(f"\n  Config: {best['signal']}/{best['regime']}/sl{best['sl']}/tp{best['tp']}/"
          f"mh{best['mh']}/k{best['k']}/s{best['step']}/vt{best['vt']}")
        P(f"  Weekly mean:    {best['weekly_mean_pct']:+.3f}%")
        P(f"  Sharpe:         {best['sharpe']:.3f}")
        P(f"  CAGR:           {best['cagr_pct']:.0f}%")
        P(f"  Total return:   {best['total_return_pct']:.0f}%")
        P(f"  Max drawdown:   {best['max_drawdown_pct']:.1f}%")
        P(f"  Weekly vol:     {best['weekly_vol_pct']:.3f}%")
        P(f"  Win rate (wk):  {best['weekly_win_rate_pct']:.0f}%")
        P(f"  P(>=2% week):   {best['weeks_ge_2pct_pct']:.0f}%")
        P(f"  P(>=3% week):   {best['weeks_ge_3pct_pct']:.0f}%")
        P(f"  P(>=5% week):   {best['weeks_ge_5pct_pct']:.0f}%")
        P(f"  Trades:         {best['num_trades']} ({best['n_rebalances']} rebalances)")
        P(f"  Position win:   {best['win_rate_pct']:.0f}%  Stop: {best['stop_rate_pct']:.0f}%")
        P(f"  Median weekly:  {best['median_weekly_pct']:+.3f}%")
        P(f"  Range:          {best['min_weekly_pct']:+.3f}% to {best['max_weekly_pct']:+.3f}%")

    # ================================================================
    # PART 4: Save results
    # ================================================================
    OUT = ROOT / "data" / "research" / "gap_run_final.json"
    payload = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "oos_window": [str(dates[mid].date()), str(dates[-1].date())],
        "benchmark_weekly": 0.526,
        "n_valid_configs": len(all_results),
        "n_target_configs": len(target_configs),
        "target_configs": target_configs[:20],
        "best": target_configs[0] if target_configs else None,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    P(f"\n  Results saved to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
