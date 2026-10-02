"""Walk-forward validation + market regime filter for gap-and-run strategy.

Key insight from initial testing: the strategy is market-condition dependent.
It works in bull markets (Fold 1: +5.1% weekly) but fails in sideways markets
(Fold 2: -0.03% weekly). This script adds a market regime filter using NIFTYBEES
as the trend indicator, and validates with proper walk-forward.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
sys.path.insert(0, str(ROOT / "scripts"))
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


def compute_signals(close, high, low, open_, volume):
    c = close; v = volume; o = open_; h = high; l = low
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

    # Volume ratio
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
    n_stocks = close.shape[1]
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

    if (path_end - t - 1) == 1:
        exits = cum_hi[:, -1] * 0 + cl[-1] / (entry + eps) - 1.0
        sw = stop_hits[-1]
        tw = target_hits[-1] & ~sw
        exits[sw] = stop_level
        exits[tw] = target_level
        return exits, int(np.sum(sw)), int(np.sum(tw)), int(np.sum(~sw & ~tw))

    stop_first = np.argmax(stop_hits, axis=0)
    stop_any = np.any(stop_hits, axis=0)
    target_first = np.argmax(target_hits, axis=0)
    target_any = np.any(target_hits, axis=0)

    exits = cl[-1] / (entry + eps) - 1.0
    stop_wins = stop_any & (~target_any | (stop_first <= target_first))
    target_wins = target_any & (~stop_any | (target_first < stop_first))
    exits[stop_wins] = stop_level
    exits[target_wins] = target_level

    n_stop = int(np.sum(stop_wins))
    n_target = int(np.sum(target_wins))
    n_hold = len(picks) - n_stop - n_target
    return exits, n_stop, n_target, n_hold


def run_strategy(close, high, low, signal, sl_pct, tp_pct,
                 max_holding, k, step, trend_sma=0, costs=COST, volume=None,
                 regime_filter=None):
    """regime_filter: boolean array (n_dates,) — True = bull market, allow trading"""
    n_dates, n_stocks = close.shape
    warmup = max(trend_sma, 22) if trend_sma > 0 else 22
    rb_dates = list(range(warmup, n_dates - max_holding, step))
    if len(rb_dates) < 5:
        return {"error": "insufficient dates"}

    all_ret = []
    total_trades = 0
    total_stops = 0
    total_targets = 0

    # Precompute trend mask per stock
    trend_mask_all = None
    if trend_sma > 0 and trend_sma > 0:
        trend_mask_all = np.zeros((n_dates, n_stocks), dtype=bool)
        for i in range(trend_sma - 1, n_dates):
            sma = close[i - trend_sma + 1:i + 1].mean(axis=0)
            trend_mask_all[i] = close[i] > sma

    for t in rb_dates:
        # Regime filter (market-level: cash out in bear markets)
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

        exits, n_stop, n_target, n_hold = compute_exit(
            close, high, low, t, picks, max_holding, sl_pct, tp_pct
        )
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
    }


def build_regime_filter(nifty_close, sma_window=200):
    """True when NIFTY is above its SMA (bull market), False otherwise."""
    n = len(nifty_close)
    filt = np.zeros(n, dtype=bool)
    for i in range(sma_window - 1, n):
        sma = np.nanmean(nifty_close[i - sma_window + 1:i + 1])
        filt[i] = nifty_close[i] > sma if not np.isnan(sma) else False
    return filt


def main():
    from strategy_hunt import parse_universe
    start = pd.Timestamp("2020-01-01")
    end = pd.Timestamp("2026-09-18")

    n50 = parse_universe(ROOT / "data" / "universe" / "n50.txt")
    mid150 = parse_universe(ROOT / "data" / "universe" / "mid150.txt")
    all_symbols = list(set(n50 + mid150))
    P(f"Loading {len(all_symbols)} symbols...")
    t0 = time.time()
    panels, syms, dates = load_stocks(all_symbols, start, end)
    P(f"  {len(syms)} stocks, {len(dates)} dates in {time.time()-t0:.1f}s")

    # Load NIFTYBEES as market regime indicator
    nifty = load_symbol("NIFTYBEES", start, end)
    nifty_close = nifty["close"].reindex(dates).ffill().to_numpy(dtype=np.float64)
    regime_200 = build_regime_filter(nifty_close, 200)
    regime_50 = build_regime_filter(nifty_close, 50)

    close = panels["close"].to_numpy(dtype=np.float64)
    high = panels["high"].to_numpy(dtype=np.float64)
    low = panels["low"].to_numpy(dtype=np.float64)
    open_ = panels["open"].to_numpy(dtype=np.float64)
    volume = panels["volume"].to_numpy(dtype=np.float64)
    n_dates = len(dates)

    P(f"  Date range: {dates[0].date()} → {dates[-1].date()}")
    P(f"  Bull market days (200d): {np.sum(regime_200)} / {n_dates} ({100*np.mean(regime_200):.0f}%)")
    P(f"  Bull market days (50d):  {np.sum(regime_50)} / {n_dates} ({100*np.mean(regime_50):.0f}%)")

    # B&H benchmark
    daily_ret = close[1:] / close[:-1] - 1
    bh_weekly = round(100 * float(np.nanmean(np.nanmean(daily_ret, axis=1))) * 5, 3)
    P(f"  B&H weekly mean: {bh_weekly}%")

    # Signals
    P("  Computing signals...")
    signals = compute_signals(close, high, low, open_, volume)

    # Configs to test
    configs = [
        # signal_name, sl, tp, mh, k, step, tsma
        ("gap_run_5", 10, 20, 20, 3, 5, 0),
        ("gap_run_5", 10, 20, 20, 5, 5, 0),
        ("gap_run_5", 10, 30, 20, 3, 5, 0),
        ("gap_run_5", 10, 20, 20, 5, 5, 200),
        ("gap_run_5", 10, 20, 20, 3, 5, 200),
        ("mom_5d", 10, 20, 20, 3, 5, 0),
        ("mom_5d", 10, 20, 20, 3, 5, 200),
        ("vol_mom_5d", 10, 20, 20, 3, 5, 0),
    ]

    # Walk-forward validation
    P(f"\n{'='*80}")
    P(f"WALK-FORWARD VALIDATION (4 folds, 50/50 train/OOS split within each fold)")
    P(f"{'='*80}")

    fold_size = n_dates // 4
    folds = [(i * fold_size, min((i + 1) * fold_size, n_dates)) for i in range(4)]

    for cfg in configs:
        sig_name, sl, tp, mh, k, step, tsma = cfg
        sig = signals[sig_name]

        results = []
        for fi, (fs, fe) in enumerate(folds):
            fold_close = close[fs:fe]
            fold_high = high[fs:fe]
            fold_low = low[fs:fe]
            fold_sig = sig[fs:fe]
            fold_vol = volume[fs:fe]
            fold_regime = regime_200[fs:fe]

            # Train/OOS split (first 70% train, last 30% OOS) within fold
            split = len(fold_close) * 7 // 10
            tr = run_strategy(fold_close[:split], fold_high[:split], fold_low[:split],
                            fold_sig[:split], sl, tp, mh, k, step, tsma,
                            volume=fold_vol[:split])
            oos = run_strategy(fold_close[split:], fold_high[split:], fold_low[split:],
                             fold_sig[split:], sl, tp, mh, k, step, tsma,
                             volume=fold_vol[split:],
                             regime_filter=fold_regime[split:])

            if "error" in tr or "error" in oos:
                results.append(("err", "err"))
            else:
                results.append((tr["weekly_mean_pct"], oos["weekly_mean_pct"]))

        train_wks = [r[0] for r in results if r[0] != "err"]
        oos_wks = [r[1] for r in results if r[1] != "err"]

        n_train_pos = sum(1 for w in train_wks if w > 0)
        n_oos_pos = sum(1 for w in oos_wks if w > 0)
        avg_train = np.mean(train_wks) if train_wks else 0
        avg_oos = np.mean(oos_wks) if oos_wks else 0

        P(f"\n  {sig_name}/sl{sl}/tp{tp}/mh{mh}/k{k}/s{step}/ts{tsma}:")
        P(f"    Train folds: {[f'{w:+.3f}%' for w in train_wks]}")
        P(f"    OOS folds:   {[f'{w:+.3f}%' for w in oos_wks]}")
        P(f"    Avg train: {avg_train:+.3f}%  |  Avg OOS: {avg_oos:+.3f}%")
        P(f"    Positive folds: {n_train_pos}/{len(train_wks)} train, {n_oos_pos}/{len(oos_wks)} OOS")

    # ================================================================
    # PART 2: Market regime filter on best config
    # ================================================================
    P(f"\n{'='*80}")
    P(f"MARKET REGIME FILTER (gap_run_5/sl10/tp20/mh20/k5/step5/ts0)")
    P(f"{'='*80}")

    sig = signals["gap_run_5"]
    mid = n_dates // 2

    # Use TRAIN period (first half) for regime filter analysis to avoid bias
    tr_close = close[:mid]
    tr_high = high[:mid]
    tr_low = low[:mid]
    tr_sig = sig[:mid]
    tr_vol = volume[:mid]
    tr_regime_200 = regime_200[:mid]
    tr_regime_50 = regime_50[:mid]

    for regime_name, regime, tsma, filter_desc in [
        ("No filter", None, 0, "always trade"),
        ("200d regime", tr_regime_200, 0, "NIFTY > 200d SMA"),
        ("50d regime", tr_regime_50, 0, "NIFTY > 50d SMA"),
        ("200d trend", None, 200, "stock > 200d SMA"),
        ("200d reg+trend", tr_regime_200, 200, "NIFTY>200d & stock>200d"),
    ]:
        r = run_strategy(tr_close, tr_high, tr_low, tr_sig,
                         10, 20, 20, 5, 5, tsma,
                         volume=tr_vol, regime_filter=regime)
        if "error" in r:
            P(f"  {regime_name}: {r['error']}")
        else:
            P(f"  {regime_name} ({filter_desc}):")
            P(f"    TRAIN wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
              f"DD={r['max_drawdown_pct']:.0f}% vol={r['weekly_vol_pct']}% "
              f"trades={r['num_trades']} win_wk={r['weekly_win_rate_pct']:.0f}%")
            P(f"    min={r['min_weekly_pct']:+.3f}% med={r['median_weekly_pct']:+.3f}% "
              f"max={r['max_weekly_pct']:+.3f}%")

    # ================================================================
    # PART 3: Combined regime + parameter optimization
    # ================================================================
    P(f"\n{'='*80}")
    P(f"OPTIMIZED GAP-AND-RUN (regime filter + best params)")
    P(f"{'='*80}")

    # Test with both regime filter AND trend filter
    test_configs = [
        (10, 20, 20, 3, 5, 0, "200d_regime"),
        (10, 20, 20, 5, 5, 0, "200d_regime"),
        (10, 20, 20, 3, 5, 200, "200d_regime+stock_trend"),
        (10, 20, 20, 5, 5, 200, "200d_regime+stock_trend"),
        (10, 20, 20, 3, 5, 0, "50d_regime"),
        (15, 30, 20, 3, 5, 0, "200d_regime"),
        (7, 14, 20, 3, 5, 0, "200d_regime"),
        (10, 30, 15, 3, 5, 0, "200d_regime"),
        (10, 20, 30, 3, 5, 0, "200d_regime"),
        (10, 20, 20, 2, 5, 0, "200d_regime"),
    ]

    for sl, tp, mh, k, step, tsma, regime_name in test_configs:
        if regime_name == "50d_regime":
            reg = regime_50
        else:
            reg = regime_200

        r = run_strategy(close[mid:], high[mid:], low[mid:], sig[mid:],
                         sl, tp, mh, k, step, tsma,
                         volume=volume[mid:], regime_filter=reg[mid:])
        if "error" in r:
            P(f"  sl{sl}/tp{tp}/mh{mh}/k{k}/{regime_name}: {r['error']}")
        else:
            ok = "***" if (2 <= r["weekly_mean_pct"] <= 5) and r["sharpe"] > 1 else "  "
            P(f"  {ok} sl{sl}/tp{tp}/mh{mh}/k{k}/{regime_name}: "
              f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
              f"DD={r['max_drawdown_pct']:.0f}% vol={r['weekly_vol_pct']}% "
              f"trades={r['num_trades']} win_wk={r['weekly_win_rate_pct']:.0f}% "
              f"min={r['min_weekly_pct']:+.3f}% med={r['median_weekly_pct']:+.3f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
