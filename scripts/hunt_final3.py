"""Final test: combined signal strategy for steady returns across all market conditions.

Tests an ensemble approach: combine gap_run signal with mom_5d signal,
using the 200d regime filter. Also tests whether a blended signal
(momentum + gap) is more robust than either alone.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
TRADING_DAYS = 252
COST = 0.00283
p = print
def P(*a, **k): k.setdefault("flush", True); p(*a, **k)

sys.path.insert(0, str(ROOT / "scripts"))


def load_symbol(sym, start, end):
    df = pd.read_parquet(DAILY / f"{sym}.parquet", columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.dropna(subset=["close"]).sort_values("ts")
    df = df[df["close"] > 0]
    df = df[(df["ts"] >= start) & (df["ts"] <= end)].set_index("ts")
    return df


def build_regime(nifty_close, window):
    n = len(nifty_close)
    filt = np.zeros(n, dtype=bool)
    for i in range(window - 1, n):
        sma = np.nanmean(nifty_close[i - window + 1:i + 1])
        filt[i] = nifty_close[i] > sma if not np.isnan(sma) else False
    return filt


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


def main():
    from strategy_hunt import parse_universe

    start = pd.Timestamp("2020-01-01")
    end = pd.Timestamp("2026-09-18")

    all_symbols = list(set(
        parse_universe(ROOT / "data" / "universe" / "n50.txt") +
        parse_universe(ROOT / "data" / "universe" / "mid150.txt")
    ))
    P(f"Loading {len(all_symbols)} symbols...")
    panels, syms, dates = load_stocks(all_symbols, start, end)
    P(f"  {len(syms)} stocks, {len(dates)} dates")

    nifty = load_symbol("NIFTYBEES", start, end)
    nif_close = nifty["close"].reindex(dates).ffill().to_numpy(dtype=np.float64)
    regime_200 = build_regime(nif_close, 200)
    regime_50 = build_regime(nif_close, 50)
    regime_dual = regime_50 & regime_200
    regime_or = regime_50 | regime_200

    close = panels["close"].to_numpy(dtype=np.float64)
    high = panels["high"].to_numpy(dtype=np.float64)
    low = panels["low"].to_numpy(dtype=np.float64)
    open_ = panels["open"].to_numpy(dtype=np.float64)
    volume = panels["volume"].to_numpy(dtype=np.float64)
    n_dates = len(dates)
    mid = n_dates // 2

    # Compute signals using the existing framework
    closes_df = panels["close"]
    highs_df = panels["high"]
    lows_df = panels["low"]
    opens_df = panels["open"]
    vols_df = panels["volume"]

    closes = closes_df.to_numpy(dtype=np.float64)
    highs = highs_df.to_numpy(dtype=np.float64)
    lows = lows_df.to_numpy(dtype=np.float64)
    opens = opens_df.to_numpy(dtype=np.float64)
    vols = vols_df.to_numpy(dtype=np.float64)

    # Build signals manually
    c = closes; v = vols; o = opens; h = highs; l = lows
    signals = {}
    for lb in [5, 10, 20]:
        s = np.full_like(c, -np.inf)
        s[lb:] = c[lb:] / c[:-lb] - 1.0
        s = np.roll(s, 1, axis=0); s[:lb + 2] = -np.inf
        signals[f"mom_{lb}d"] = s
    gap = np.full_like(c, -np.inf)
    gap[1:] = (o[1:] - c[:-1]) / c[:-1]
    gap = np.roll(gap, 1, axis=0); gap[:2] = -np.inf
    signals["gap_run_5"] = np.where(gap > 0.02, gap * signals["mom_5d"], -np.inf)
    signals["gap_run"] = np.where(gap > 0.01, gap * signals["mom_5d"], -np.inf)
    vol_avg = np.full_like(v, np.nan)
    for i in range(20, len(v)):
        vol_avg[i] = v[i - 20:i].mean(axis=0)
    vr = np.where(vol_avg > 0, v / vol_avg, 0)

    # Blended signal: 70% gap_run_5 + 30% mom_5d * vol_ratio
    # This combines the strength of gap-and-run with the consistency of momentum
    gap_signal = signals["gap_run_5"]
    mom_signal = signals["mom_5d"]
    # Only use gap_run where gap exists, otherwise fall back to momentum
    blended = np.where(gap > 0.02, gap * signals["mom_5d"], -np.inf)
    # Where gap doesn't exist, use momentum * volume_ratio as fallback
    momentum_fallback = np.where(vr > 1.0, signals["mom_5d"] * (1 + vr), -np.inf)
    # Combine: use gap_run_5 when available, otherwise momentum fallback
    # Weight: gap signal is stronger (3x), momentum is weaker (1x)
    combined = np.where(gap > 0.02, gap * signals["mom_5d"] * 3, momentum_fallback)

    signals["combined"] = combined
    signals["gap_vol"] = np.where((gap > 0.015) & (vr > 1.0), gap * signals["mom_5d"] * vr, -np.inf)

    daily_ret = np.zeros_like(c)
    daily_ret[1:] = c[1:] / c[:-1] - 1.0

    # ================================================================
    # 3-FOLD WALK-FORWARD for top configs
    # ================================================================
    P(f"\n{'='*80}")
    P(f"3-FOLD WALK-FORWARD (all market sub-periods, OOS)")
    P(f"Comparing: gap_run_5 vs combined vs gap_vol")
    P(f"{'='*80}")

    configs = [
        ("gap_run_5", {"regime": regime_200, "name": "200d",
                        "sl": 10, "tp": 20, "mh": 20, "k": 5, "vt": 0.2}),
        ("gap_run_5", {"regime": None, "name": "none",
                        "sl": 10, "tp": 20, "mh": 20, "k": 5, "vt": 0.0}),
        ("combined", {"regime": regime_200, "name": "200d",
                       "sl": 10, "tp": 20, "mh": 20, "k": 5, "vt": 0.2}),
        ("combined", {"regime": regime_or, "name": "or_50_200",
                       "sl": 10, "tp": 20, "mh": 20, "k": 5, "vt": 0.2}),
        ("gap_vol", {"regime": regime_200, "name": "200d",
                      "sl": 15, "tp": 30, "mh": 20, "k": 3, "vt": 0.2}),
        ("gap_vol", {"regime": regime_or, "name": "or_50_200",
                      "sl": 10, "tp": 20, "mh": 20, "k": 5, "vt": 0.0}),
    ]

    for sig_name, cfg in configs:
        sig = signals[sig_name]
        fold_size = (n_dates - mid) // 3
        for fi in range(3):
            s_idx = mid + fi * fold_size
            e_idx = mid + (fi + 1) * fold_size if fi < 2 else n_dates
            reg = cfg["regime"]
            r = run_local_strategy(
                close[s_idx:e_idx], high[s_idx:e_idx], low[s_idx:e_idx],
                sig[s_idx:e_idx], cfg["sl"], cfg["tp"], cfg["mh"],
                cfg["k"], 5, costs=COST,
                regime_filter=reg[s_idx:e_idx] if reg is not None else None,
                vol_target=cfg["vt"]
            )
            if "error" in r:
                status = "insufficient"
            else:
                status = f"wk={r['weekly_mean_pct']:+.3f}% Sharp={r['sharpe']:.2f} DD={r['max_drawdown_pct']:.0f}% rb={r['n_rebalances']}"
            P(f"  {sig_name}/{cfg['name']}/sl{cfg['sl']}/tp{cfg['tp']}/k{cfg['k']}/vt{cfg['vt']} "
              f"F{fi+1} ({dates[s_idx].date()}→{dates[e_idx-1].date()}): {status}")

    # ================================================================
    # PART 2: Full OOS results for comparison table
    # ================================================================
    P(f"\n{'='*80}")
    P(f"FULL OOS COMPARISON (2023-05 → 2026-09)")
    P(f"{'='*80}")

    P(f"\n  {'Config':50s} {'wk%':>8s} {'Sharpe':>7s} {'DD%':>6s} {'rb':>4s} {'pos_wk':>7s} {'P(>=5%)':>7s}")
    P(f"  {'-'*50} {'-'*8} {'-'*7} {'-'*6} {'-'*4} {'-'*7} {'-'*7}")

    all_configs = []
    for sig_name in ["gap_run_5", "gap_run", "combined", "gap_vol", "mom_5d"]:
        for reg_name, reg in [("none", None), ("50d", regime_50[mid:]), ("200d", regime_200[mid:]),
                               ("dual", regime_dual[mid:]), ("or", regime_or[mid:])]:
            for sl, tp, mh, k, vt in [(10, 20, 20, 5, 0.0), (10, 20, 20, 5, 0.2),
                                       (10, 20, 20, 3, 0.2), (15, 30, 30, 3, 0.2),
                                       (10, 30, 20, 5, 0.2), (15, 25, 30, 5, 0.2),
                                       (12, 24, 20, 5, 0.2), (7, 14, 20, 5, 0.0)]:
                r = run_local_strategy(
                    close[mid:], high[mid:], low[mid:], signals[sig_name][mid:],
                    sl, tp, mh, k, 5, costs=COST,
                    regime_filter=reg, vol_target=vt
                )
                if "error" in r:
                    continue
                if r["max_drawdown_pct"] < -30:
                    continue
                if r["n_rebalances"] < 10:
                    continue
                all_configs.append({
                    "signal": sig_name, "regime": reg_name,
                    "sl": sl, "tp": tp, "mh": mh, "k": k, "vt": vt, **r
                })

    # In target range
    target = [r for r in all_configs if 2 <= r["weekly_mean_pct"] <= 5 and r["sharpe"] > 2]
    target.sort(key=lambda r: (r["sharpe"], r["n_rebalances"]), reverse=True)

    for r in target[:20]:
        label = f"{r['signal']}/{r['regime']}/sl{r['sl']}/tp{r['tp']}/mh{r['mh']}/k{r['k']}/vt{r['vt']}"
        P(f"  {label:50s} {r['weekly_mean_pct']:>+8.3f} {r['sharpe']:>7.3f} {r['max_drawdown_pct']:>6.0f} "
          f"{r['n_rebalances']:>4d} {r['weekly_win_rate_pct']:>6.0f}% {r['weeks_ge_5pct_pct']:>6.0f}%")

    # ================================================================
    # PART 3: Best config detailed analysis
    # ================================================================
    P(f"\n{'='*80}")
    P(f"BEST CONFIG DETAILED ANALYSIS")
    P(f"{'='*80}")

    # Pick: highest Sharpe with >= 20 rebalances and in 2-5% range
    best_candidates = [r for r in target if r["n_rebalances"] >= 18]
    if not best_candidates:
        best_candidates = target

    best = max(best_candidates, key=lambda r: r["sharpe"])
    sig_name = best["signal"]
    P(f"\n  Best: {sig_name}/{best['regime']}/sl{best['sl']}/tp{best['tp']}/"
      f"mh{best['mh']}/k{best['k']}/vt{best['vt']}")
    P(f"  OOS: {dates[mid].date()} → {dates[-1].date()}")
    P(f"    Weekly mean:    {best['weekly_mean_pct']:+.3f}%")
    P(f"    Sharpe:         {best['sharpe']:.3f}")
    P(f"    CAGR:           {best['cagr_pct']:.0f}%")
    P(f"    Total return:   {best['total_return_pct']:.0f}%")
    P(f"    Max drawdown:   {best['max_drawdown_pct']:.1f}%")
    P(f"    Weekly vol:     {best['weekly_vol_pct']:.3f}%")
    P(f"    Win rate (wk):  {best['weekly_win_rate_pct']:.0f}%")
    P(f"    P(>=2% week):   {best['weeks_ge_2pct_pct']:.0f}%")
    P(f"    P(>=3% week):   {best['weeks_ge_3pct_pct']:.0f}%")
    P(f"    P(>=5% week):   {best['weeks_ge_5pct_pct']:.0f}%")
    P(f"    Rebalances:     {best['n_rebalances']} ({best['num_trades']} trades)")
    P(f"    Position win:   {best['win_rate_pct']:.0f}%  Stop: {best['stop_rate_pct']:.0f}%")
    P(f"    Median weekly:  {best['median_weekly_pct']:+.3f}%")
    P(f"    Min weekly:     {best['min_weekly_pct']:+.3f}%")
    P(f"    Max weekly:     {best['max_weekly_pct']:+.3f}%")

    # Save
    OUT = ROOT / "data" / "research" / "gap_run_final_v3.json"
    payload = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "oos_window": [str(dates[mid].date()), str(dates[-1].date())],
        "n_valid_configs": len(all_configs),
        "n_target_configs": len(target),
        "target_configs": target[:20],
        "best": best,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    P(f"\n  Results saved to {OUT}")
    return 0


def run_local_strategy(close, high, low, signal, sl_pct, tp_pct, max_holding, k, step,
                       costs=COST, regime_filter=None, vol_target=0.20, vol_window=20,
                       min_rebalances=5):
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
        if vol_target > 0:
            avg_vol = np.mean(stock_vol)
            vol_scale = min(1.0, vol_target / avg_vol) if avg_vol > 0 else 1.0
        else:
            vol_scale = 1.0
        net = exits - costs
        portfolio_ret = float(np.sum(raw_weights * vol_scale * net))
        all_ret.append(portfolio_ret)
        total_trades += k
        total_stops += n_stop
        total_targets += n_target

    if len(all_ret) < min_rebalances:
        return {"error": f"insufficient trades ({len(all_ret)} < {min_rebalances})"}
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


if __name__ == "__main__":
    raise SystemExit(main())
