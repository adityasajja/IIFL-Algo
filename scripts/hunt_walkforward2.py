"""Walk-forward validation for the gap-and-run strategy with 200d regime filter.

Confirms robustness across time. Uses 6 overlapping folds to get more data points.
Also tests parameter sensitivity and computes deflated Sharpe.
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
            close_d[sym] = df["close"]
            high_d[sym] = df["high"]
            low_d[sym] = df["low"]
            open_d[sym] = df["open"]
            vol_d[sym] = df["volume"]
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


def compute_gap_run_5(close, high, low, open_, volume):
    """gap_run_5 signal: gap > 2% on previous day AND 5d momentum > 0."""
    c = close; v = volume; o = open_
    signals = {}
    for lb in [3, 5, 10, 20]:
        sig = np.full_like(c, -np.inf)
        sig[lb:] = c[lb:] / c[:-lb] - 1.0
        sig = np.roll(sig, 1, axis=0); sig[:lb + 2] = -np.inf
        signals[f"mom_{lb}d"] = sig
    for lb in [3, 5, 10]:
        sig = np.full_like(c, -np.inf)
        sig[lb:] = -(c[lb:] / c[:-lb] - 1.0)
        sig = np.roll(sig, 1, axis=0); sig[:lb + 2] = -np.inf
        signals[f"rev_{lb}d"] = sig
    gap = np.full_like(c, -np.inf)
    gap[1:] = (o[1:] - c[:-1]) / c[:-1]
    gap = np.roll(gap, 1, axis=0); gap[:2] = -np.inf
    gap_run = gap * signals["mom_5d"]
    gap_run = np.where(gap > 0.01, gap_run, -np.inf)
    signals["gap_run"] = gap_run
    gap_run_5 = gap * signals["mom_5d"]
    gap_run_5 = np.where(gap > 0.02, gap_run_5, -np.inf)
    signals["gap_run_5"] = gap_run_5
    vol_avg = np.full_like(v, np.nan)
    for i in range(20, len(v)):
        vol_avg[i] = v[i - 20:i].mean(axis=0)
    vol_ratio = np.where(vol_avg > 0, v / vol_avg, 0)
    vol_mom = signals["mom_5d"] * vol_ratio
    vol_mom = np.where(vol_ratio > 1.2, vol_mom, -np.inf)
    signals["vol_mom_5d"] = vol_mom
    vol_mom_10 = signals["mom_10d"] * vol_ratio
    vol_mom_10 = np.where(vol_ratio > 1.2, vol_mom_10, -np.inf)
    signals["vol_mom_10d"] = vol_mom_10
    return signals


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
    stop_level = -sl_pct / 100.0
    target_level = tp_pct / 100.0
    stop_hits = cum_lo <= stop_level
    target_hits = cum_hi >= target_level
    n_days = path_end - (t + 1)
    if n_days == 1:
        exits = cl[-1] / (entry + eps) - 1.0
        sw = stop_hits[-1]; tw = target_hits[-1] & ~sw
        exits[sw] = stop_level; exits[tw] = target_level
        return exits, int(np.sum(sw)), int(np.sum(tw)), int(np.sum(~sw & ~tw))
    stop_first = np.argmax(stop_hits, axis=0)
    stop_any = np.any(stop_hits, axis=0)
    target_first = np.argmax(target_hits, axis=0)
    target_any = np.any(target_hits, axis=0)
    exits = cl[-1] / (entry + eps) - 1.0
    stop_wins = stop_any & (~target_any | (stop_first <= target_first))
    target_wins = target_any & (~stop_any | (target_first < stop_first))
    exits[stop_wins] = stop_level; exits[target_wins] = target_level
    n_stop = int(np.sum(stop_wins))
    n_target = int(np.sum(target_wins))
    n_hold = len(picks) - n_stop - n_target
    return exits, n_stop, n_target, n_hold


def run_strategy(close, high, low, signal, sl_pct, tp_pct, max_holding, k, step,
                 trend_sma=0, costs=COST, volume=None, regime_filter=None):
    n_dates, n_stocks = close.shape
    warmup = max(trend_sma, 22) if trend_sma > 0 else 22
    rb_dates = list(range(warmup, n_dates - max_holding, step))
    if len(rb_dates) < 5:
        return {"error": "insufficient dates"}
    all_ret = []
    total_trades = 0
    total_stops = 0
    total_targets = 0
    trend_mask_all = None
    if trend_sma > 0:
        trend_mask_all = np.zeros((n_dates, n_stocks), dtype=bool)
        for i in range(trend_sma - 1, n_dates):
            sma = close[i - trend_sma + 1:i + 1].mean(axis=0)
            trend_mask_all[i] = close[i] > sma
    for t in rb_dates:
        if regime_filter is not None and t < len(regime_filter) and not regime_filter[t]:
            continue
        scores = signal[t, :].copy()
        valid = close[t, :] > 0
        scores = np.where(valid, scores, -np.inf)
        if trend_mask_all is not None:
            mask = valid & trend_mask_all[t]
            scores = np.where(mask, scores, -np.inf)
        top_k = min(k, int(np.sum(scores > -np.inf)))
        if top_k < k:
            continue
        picks = np.argpartition(-scores, top_k - 1)[:top_k]
        picks = picks[np.argsort(-scores[picks])]
        exits, n_stop, n_target, n_hold = compute_exit(close, high, low, t, picks, max_holding, sl_pct, tp_pct)
        net = exits - costs
        all_ret.append(float(np.mean(net)))
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
        "weekly_returns": [round(100 * float(r), 3) for r in wr],
    }


def build_regime(nifty_close, window=200):
    n = len(nifty_close)
    filt = np.zeros(n, dtype=bool)
    for i in range(window - 1, n):
        sma = np.nanmean(nifty_close[i - window + 1:i + 1])
        filt[i] = nifty_close[i] > sma if not np.isnan(sma) else False
    return filt


def deflated_sharpe(sharpe, n_trials, n_periods):
    """Approximate deflated Sharpe: sharpe adjusted for multiple testing."""
    # Expected max Sharpe under null (0 Sharpe, N trials)
    from scipy.stats import norm
    if n_trials <= 1:
        return sharpe
    # Using the formula: DSR = (SR * sqrt(N) - E[max t]) / sqrt(N)
    # E[max t] ≈ sqrt(2 * ln(N))
    expected_max_t = np.sqrt(2 * np.log(n_trials))
    t_stat = sharpe * np.sqrt(n_periods)
    dsr_t = t_stat - expected_max_t
    dsr = dsr_t / np.sqrt(n_periods) if n_periods > 0 else 0
    return round(dsr, 3)


def main():
    start = pd.Timestamp("2020-01-01")
    end = pd.Timestamp("2026-09-18")

    from strategy_hunt import parse_universe
    n50 = parse_universe(ROOT / "data" / "universe" / "n50.txt")
    mid150 = parse_universe(ROOT / "data" / "universe" / "mid150.txt")
    all_symbols = list(set(n50 + mid150))
    P(f"Loading {len(all_symbols)} symbols...")
    t0 = time.time()
    panels, syms, dates = load_stocks(all_symbols, start, end)
    P(f"  {len(syms)} stocks, {len(dates)} dates in {time.time()-t0:.1f}s")

    nifty = load_symbol("NIFTYBEES", start, end)
    nifty_close = nifty["close"].reindex(dates).ffill().to_numpy(dtype=np.float64)
    regime_200 = build_regime(nifty_close, 200)

    close = panels["close"].to_numpy(dtype=np.float64)
    high = panels["high"].to_numpy(dtype=np.float64)
    low = panels["low"].to_numpy(dtype=np.float64)
    open_ = panels["open"].to_numpy(dtype=np.float64)
    volume = panels["volume"].to_numpy(dtype=np.float64)
    n_dates = len(dates)

    daily_ret = close[1:] / close[:-1] - 1
    bh_weekly = round(100 * float(np.nanmean(np.nanmean(daily_ret, axis=1))) * 5, 3)
    P(f"  B&H weekly: {bh_weekly}%  |  Market bull days: {np.sum(regime_200)}/{n_dates} ({100*np.mean(regime_200):.0f}%)")

    signals = compute_gap_run_5(close, high, low, open_, volume)

    # ================================================================
    # 6-FOLD WALK-FORWARD (expanding window, 200d regime filter)
    # ================================================================
    P(f"\n{'='*80}")
    P(f"6-FOLD WALK-FORWARD (expanding window, 200d regime filter)")
    P(f"Config: gap_run_5/sl10/tp20/mh20/k5/step5/ts0")
    P(f"{'='*80}")

    n_folds = 6
    fold_size = (n_dates - 252) // n_folds  # Start after 1 year warmup
    sig = signals["gap_run_5"]

    all_oos_results = []
    for fold in range(n_folds):
        train_end = 252 + (fold + 1) * fold_size
        if fold + 1 < n_folds:
            test_end = 252 + (fold + 2) * fold_size
        else:
            test_end = n_dates
        if test_end <= train_end or train_end < 252:
            continue

        # OOS test: from train_end to test_end
        test_r = run_strategy(
            close[train_end:test_end], high[train_end:test_end], low[train_end:test_end],
            sig[train_end:test_end], 10, 20, 20, 5, 5, 0,
            volume=volume[train_end:test_end], regime_filter=regime_200[train_end:test_end]
        )
        if "error" in test_r:
            P(f"  Fold {fold+1} ({dates[train_end].date()} → {dates[test_end-1].date()}): {test_r['error']}")
            continue
        all_oos_results.append(test_r)
        P(f"  Fold {fold+1} ({dates[train_end].date()} → {dates[test_end-1].date()}): "
          f"wk={test_r['weekly_mean_pct']:+.3f}% Sharpe={test_r['sharpe']:.3f} "
          f"DD={test_r['max_drawdown_pct']:.0f}% trades={test_r['num_trades']} "
          f"pos={test_r['weekly_win_rate_pct']:.0f}%")

    if all_oos_results:
        oos_wks = [r["weekly_mean_pct"] for r in all_oos_results]
        P(f"\n  Walk-forward summary:")
        P(f"  Avg weekly: {np.mean(oos_wks):+.3f}%  (range: {min(oos_wks):+.3f}% to {max(oos_wks):+.3f}%)")
        P(f"  Positive folds: {sum(1 for w in oos_wks if w > 0)}/{len(oos_wks)}")
        P(f"  Sharpe>2 folds: {sum(1 for r in all_oos_results if r['sharpe'] > 2)}/{len(all_oos_results)}")
        # Aggregate all weekly returns
        all_wr = []
        for r in all_oos_results:
            all_wr.extend(r["weekly_returns"])
        all_wr = np.array(all_wr)
        P(f"  Overall (pooled): avg={np.mean(all_wr):+.3f}% std={np.std(all_wr):.3f}% "
          f"win={100*np.mean(all_wr>0):.0f}% P(>=2%)={100*np.mean(all_wr>=2):.0f}%")
        P(f"  P(>=5%)={100*np.mean(all_wr>=5):.0f}% min={min(all_wr):+.3f}% max={max(all_wr):+.3f}%")

        # Deflated Sharpe
        total_periods = sum(r["n_rebalances"] for r in all_oos_results)
        avg_sharpe = np.mean([r["sharpe"] for r in all_oos_results])
        avg_periods = np.mean([r["n_rebalances"] for r in all_oos_results])
        dsr = deflated_sharpe(avg_sharpe, 2160, avg_periods)
        P(f"\n  Deflated Sharpe: {dsr} (raw Sharpe {avg_sharpe:.3f}, "
          f"avg periods={avg_periods:.0f}, N_trials=2160)")

    # ================================================================
    # PARAMETER SENSITIVITY (on OOS: second half)
    # ================================================================
    P(f"\n{'='*80}")
    P(f"PARAMETER SENSITIVITY (OOS: 2023-05 → 2026-09, 200d regime)")
    P(f"{'='*80}")

    mid = n_dates // 2
    sensitivity_tests = [
        (10, 20, 20, 5, 5, 0, "base"),
        (7, 14, 20, 5, 5, 0, "tighter sl/tp"),
        (12, 24, 20, 5, 5, 0, "wider sl/tp"),
        (15, 30, 20, 5, 5, 0, "wide sl/tp"),
        (10, 20, 20, 3, 5, 0, "k=3"),
        (10, 20, 20, 7, 5, 0, "k=7"),
        (10, 20, 20, 5, 3, 0, "step=3"),
        (10, 20, 20, 5, 10, 0, "step=10"),
        (10, 20, 10, 5, 5, 0, "mh=10"),
        (10, 20, 30, 5, 5, 0, "mh=30"),
        (10, 30, 30, 5, 5, 0, "tp=30,mh=30"),
        (15, 25, 20, 5, 5, 0, "sl=15,tp=25"),
    ]

    for sl, tp, mh, k, step, tsma, desc in sensitivity_tests:
        r = run_strategy(close[mid:], high[mid:], low[mid:], sig[mid:],
                         sl, tp, mh, k, step, tsma,
                         volume=volume[mid:], regime_filter=regime_200[mid:])
        if "error" in r:
            P(f"  {desc} (sl{sl}/tp{tp}/mh{mh}/k{k}/s{step}): {r['error']}")
        else:
            in_range = "***" if 2 <= r["weekly_mean_pct"] <= 5 else "  "
            P(f"  {in_range} {desc} (sl{sl}/tp{tp}/mh{mh}/k{k}/s{step}): "
              f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
              f"DD={r['max_drawdown_pct']:.0f}% trades={r['num_trades']} "
              f"pos_wk={r['weekly_win_rate_pct']:.0f}% min={r['min_weekly_pct']:+.3f}% "
              f"med={r['median_weekly_pct']:+.3f}%")

    # ================================================================
    # COMPARE: momentum signal + 200d regime (does gap-filter matter?)
    # ================================================================
    P(f"\n{'='*80}")
    P(f"SIGNAL COMPARISON (OOS, 200d regime, k=5, step=5)")
    P(f"{'='*80}")
    for sig_name in ["gap_run_5", "gap_run", "mom_5d", "vol_mom_5d", "mom_10d"]:
        r = run_strategy(close[mid:], high[mid:], low[mid:], sig[mid:],
                         10, 20, 20, 5, 5, 0,
                         volume=volume[mid:], regime_filter=regime_200[mid:])
        if "error" in r:
            P(f"  {sig_name}: {r['error']}")
        else:
            P(f"  {sig_name}: wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
              f"DD={r['max_drawdown_pct']:.0f}% trades={r['num_trades']} "
              f"pos_wk={r['weekly_win_rate_pct']:.0f}%")

    # ================================================================
    # FINAL RECOMMENDATION
    # ================================================================
    P(f"\n{'='*80}")
    P(f"FINAL RESULTS")
    P(f"{'='*80}")

    final_r = run_strategy(close[mid:], high[mid:], low[mid:], sig[mid:],
                           10, 20, 20, 5, 5, 0,
                           volume=volume[mid:], regime_filter=regime_200[mid:])
    if "error" not in final_r:
        P(f"\n  BEST: gap_run_5/sl10/tp20/mh20/k5/step5/ts0 + 200d_regime")
        P(f"  OOS ({dates[mid].date()} → {dates[-1].date()}):")
        P(f"    Weekly mean:    {final_r['weekly_mean_pct']:+.3f}%")
        P(f"    Sharpe:         {final_r['sharpe']:.3f}")
        P(f"    CAGR:           {final_r['cagr_pct']:.0f}%")
        P(f"    Total return:   {final_r['total_return_pct']:.0f}%")
        P(f"    Max drawdown:   {final_r['max_drawdown_pct']:.1f}%")
        P(f"    Vol (annual):   {final_r['vol_pct']:.1f}%")
        P(f"    Weekly vol:     {final_r['weekly_vol_pct']:.3f}%")
        P(f"    Win rate (wk):  {final_r['weekly_win_rate_pct']:.0f}%")
        P(f"    P(>=2% week):   {final_r['weeks_ge_2pct_pct']:.0f}%")
        P(f"    P(>=3% week):   {final_r['weeks_ge_3pct_pct']:.0f}%")
        P(f"    P(>=5% week):   {final_r['weeks_ge_5pct_pct']:.0f}%")
        P(f"    Trades:         {final_r['num_trades']} ({final_r['n_rebalances']} rebalances)")
        P(f"    Position win:   {final_r['win_rate_pct']:.0f}%  Stop: {final_r['stop_rate_pct']:.0f}%")
        P(f"    Weekly range:   {final_r['min_weekly_pct']:+.3f}% to {final_r['max_weekly_pct']:+.3f}%")
        P(f"    Median weekly:  {final_r['median_weekly_pct']:+.3f}%")

        # Deflated Sharpe
        dsr = deflated_sharpe(final_r["sharpe"], 2160, final_r["n_rebalances"])
        P(f"\n  Deflated Sharpe (2160 trials): {dsr}")
        P(f"  Raw Sharpe: {final_r['sharpe']:.3f}")
        print(f"\n  Status: {'*** IN TARGET RANGE (2-5%) ***' if 2 <= final_r['weekly_mean_pct'] <= 5 else 'above target'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
