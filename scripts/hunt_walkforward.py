"""Walk-forward validation + volatility targeting for the gap-and-run strategy.

Key diagnostics:
1. Walk-forward: test the best config across multiple time folds
2. Volatility targeting: scale positions inversely to ATR
3. Parameter robustness: test neighboring configs
4. Monthly return decomposition
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
from itertools import product

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import strategy_hunt as sh
exec(open(ROOT / "scripts" / "hunt_stops.py").read().split("def main")[0])  # reuse all functions

DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
TRADING_DAYS = 252
COST_PER_ROUNDTRIP = 0.00283
p = print

def P(*a, **k):
    k.setdefault("flush", True); p(*a, **k)


def run_with_vol_targeting(close, high, low, signal, stop_pct, target_pct,
                            max_holding, k, step, trend_sma=0, costs=0.00283,
                            vol_window=20, vol_target_annual=0.30, returns_array=None):
    """Run strategy with volatility targeting: scale position size inversely to
    recent realized volatility, targeting a fixed annual vol."""
    n_dates, n_stocks = close.shape
    warmup = max(trend_sma, 22) if trend_sma > 0 else 22
    rebalance_dates = list(range(warmup, n_dates - max_holding, step))
    if len(rebalance_dates) < 5:
        return {"error": "insufficient rebalancing dates"}

    # Precompute daily returns
    daily_ret = np.full_like(close, 0.0)
    daily_ret[1:] = close[1:] / close[:-1] - 1.0

    all_returns = []
    total_trades = 0
    total_stops = 0
    total_targets = 0

    for t in rebalance_dates:
        scores = signal[t, :].copy()
        valid = close[t, :] > 0
        scores = np.where(valid, scores, -np.inf)

        if trend_sma > 0:
            trend = close[t] > close[t - trend_sma] if t >= trend_sma else True
            # Compute SMA for each stock
            sma = np.full(n_stocks, np.nan)
            if t >= trend_sma:
                sma = close[t - trend_sma + 1:t + 1].mean(axis=0)
            mask = valid & (close[t] > sma)
            scores = np.where(mask, scores, -np.inf)

        top_k = min(k, int(np.sum(scores > -np.inf)))
        if top_k < k:
            continue

        picks = np.argpartition(-scores, top_k - 1)[:top_k]
        picks = picks[np.argsort(-scores[picks])]

        # Volatility targeting: compute realized vol for each picked stock
        vol_lookback = min(vol_window, t - 1)
        if vol_lookback > 2:
            recent_ret = daily_ret[t - vol_lookback:t, :][:, picks]
            stock_vol = np.std(recent_ret, axis=0, ddof=1) * np.sqrt(TRADING_DAYS)
        else:
            stock_vol = np.full(len(picks), 0.20)  # Default 20% annual vol

        # Target volatility scaling: weight inversely to stock vol
        inv_vol = np.where(stock_vol > 0.001, 1.0 / stock_vol, 1.0 / 0.20)
        # Normalize to equal weight, then scale by vol target
        raw_weights = inv_vol / inv_vol.sum() if inv_vol.sum() > 0 else np.ones(k) / k

        # Scale by target vol: if market vol is high, reduce size
        # Use average stock vol to determine scaling
        avg_vol = np.mean(stock_vol)
        vol_scale = min(1.0, vol_target_annual / avg_vol) if avg_vol > 0 else 1.0

        # Compute exit returns for each position
        exit_returns, n_stop, n_target, n_hold = compute_exit_returns(
            close, high, low, t, picks, max_holding, stop_pct, target_pct
        )

        # Apply position weights and vol targeting
        net_returns = exit_returns - costs
        # Weighted return: sum of (weight * return)
        portfolio_return = float(np.sum(raw_weights * vol_scale * net_returns))

        all_returns.append(portfolio_return)
        total_trades += k
        total_stops += n_stop
        total_targets += n_target

    if len(all_returns) < 5:
        return {"error": "insufficient trading days"}

    returns = np.array(all_returns)
    cum_ret = np.cumprod(1 + returns)
    total_return = float(cum_ret[-1] - 1)

    n_periods = len(returns)
    years = n_periods * step / TRADING_DAYS
    cagr = (1 + total_return) ** (1.0 / years) - 1 if years > 0 else 0
    periods_per_year = TRADING_DAYS / step
    arith_annual = float(np.mean(returns)) * periods_per_year
    vol = float(np.std(returns, ddof=1)) * np.sqrt(periods_per_year) if n_periods > 1 else 0
    sharpe = (arith_annual - 0.075) / vol if vol > 0 else 0

    weekly_factor = 5.0 / step
    wr = returns * weekly_factor
    weekly_mean = round(100 * float(np.mean(wr)), 3)
    weekly_std = round(100 * float(np.std(wr, ddof=1)), 3) if len(wr) > 1 else 0
    weekly_win = round(100 * float(np.mean(wr > 0)), 1)
    weekly_ge2 = round(100 * float(np.mean(wr >= 0.02)), 1)
    weekly_ge3 = round(100 * float(np.mean(wr >= 0.03)), 1)
    weekly_ge5 = round(100 * float(np.mean(wr >= 0.05)), 1)

    running_max = np.maximum.accumulate(cum_ret)
    max_dd = float(np.min(cum_ret / running_max - 1)) if len(cum_ret) > 0 else 0

    position_win_rate = round(100 * total_targets / max(total_trades, 1), 1)
    position_stop_rate = round(100 * total_stops / max(total_trades, 1), 1)

    return {
        "cagr_pct": round(100 * cagr, 2),
        "sharpe": round(sharpe, 3),
        "vol_pct": round(vol, 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
        "total_return_pct": round(100 * total_return, 2),
        "weekly_mean_pct": weekly_mean,
        "weekly_vol_pct": weekly_std,
        "weekly_win_rate_pct": weekly_win,
        "weeks_ge_2pct_pct": weekly_ge2,
        "weeks_ge_3pct_pct": weekly_ge3,
        "weeks_ge_5pct_pct": weekly_ge5,
        "num_trades": total_trades,
        "win_rate_pct": position_win_rate,
        "stop_rate_pct": position_stop_rate,
        "n_rebalances": n_periods,
    }


def main():
    start = pd.Timestamp("2020-01-01")
    end = pd.Timestamp("2026-09-18")

    all_symbols = list(set(
        sh.parse_universe(ROOT / "data" / "universe" / "n50.txt") +
        sh.parse_universe(ROOT / "data" / "universe" / "mid150.txt")
    ))
    P(f"Loading OHLCV for {len(all_symbols)} symbols...")
    t0 = time.time()
    panels, symbols_loaded, dates = load_panel(all_symbols, start, end, min_bars=250)
    P(f"  Loaded {len(symbols_loaded)} stocks, {len(dates)} dates in {time.time()-t0:.1f}s")

    close = panels["close"].to_numpy(dtype=np.float64)
    high = panels["high"].to_numpy(dtype=np.float64)
    low = panels["low"].to_numpy(dtype=np.float64)
    open_ = panels["open"].to_numpy(dtype=np.float64)
    volume = panels["volume"].to_numpy(dtype=np.float64)

    n_dates = len(dates)
    P(f"\n  Full range: {dates[0].date()} → {dates[-1].date()} ({n_dates} bars)")

    # Precompute signals
    signals = compute_signals(close, high, low, open_, volume, len(symbols_loaded))

    # B&H benchmark (full period)
    oos_ret = np.nanmean(close[1:] / close[:-1] - 1, axis=1)
    bh_weekly = round(100 * float(np.nanmean(oos_ret)) * 5, 3)
    P(f"\n  B&H (full, eq-weight): ~{bh_weekly}% weekly mean")

    # ================================================================
    # PART 1: Walk-forward validation of best config
    # ================================================================
    best_config = {
        "signal": "gap_run_5",
        "sl": 10, "tp": 20, "mh": 20, "k": 5, "step": 5, "tsma": 0,
    }
    sig = signals["gap_run_5"]

    P(f"\n{'='*72}")
    P(f"WALK-FORWARD VALIDATION: {best_config['signal']}/sl{best_config['sl']}/tp{best_config['tp']}/"
      f"mh{best_config['mh']}/k{best_config['k']}/step{best_config['step']}/ts{best_config['tsma']}")
    P(f"{'='*72}")

    # Split into 4 folds
    fold_size = n_dates // 4
    folds = [
        (0, fold_size),
        (fold_size, 2 * fold_size),
        (2 * fold_size, 3 * fold_size),
        (3 * fold_size, n_dates),
    ]

    fold_results = []
    for i, (start_idx, end_idx) in enumerate(folds):
        fold_close = close[start_idx:end_idx + 1]
        fold_high = high[start_idx:end_idx + 1]
        fold_low = low[start_idx:end_idx + 1]
        fold_sig = sig[start_idx:end_idx + 1]
        fold_vol = volume[start_idx:end_idx + 1]

        r = run_strategy(fold_close, fold_high, fold_low, fold_sig,
                         best_config["sl"], best_config["tp"], best_config["mh"],
                         best_config["k"], best_config["step"], best_config["tsma"],
                         volume=fold_vol)
        if "error" in r:
            P(f"  Fold {i+1} ({dates[start_idx].date()} → {dates[end_idx].date()}): {r['error']}")
            continue
        fold_results.append(r)
        P(f"  Fold {i+1} ({dates[start_idx].date()} → {dates[end_idx].date()}): "
          f"wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} DD={r['max_drawdown_pct']:.0f}% "
          f"trades={r['num_trades']} win={r['win_rate_pct']:.0f}%")

    if len(fold_results) >= 2:
        avg_wk = np.mean([r["weekly_mean_pct"] for r in fold_results])
        avg_sh = np.mean([r["sharpe"] for r in fold_results])
        P(f"\n  Walk-forward avg: wk={avg_wk:+.3f}% Sharpe={avg_sh:.3f}")
        P(f"  Consistent profitable folds: {sum(1 for r in fold_results if r['weekly_mean_pct'] > 0)}/{len(fold_results)}")
        P(f"  Consistent Sharpe>2 folds: {sum(1 for r in fold_results if r['sharpe'] > 2)}/{len(fold_results)}")

    # ================================================================
    # PART 2: Volatility targeting vs equal weighting
    # ================================================================
    P(f"\n{'='*72}")
    P(f"VOLATILITY TARGETING vs EQUAL WEIGHTING")
    P(f"{'='*72}")

    configs_to_test = [
        best_config,
        {"signal": "gap_run_5", "sl": 10, "tp": 20, "mh": 20, "k": 3, "step": 5, "tsma": 0},
        {"signal": "gap_run_5", "sl": 10, "tp": 20, "mh": 20, "k": 5, "step": 5, "tsma": 200},
    ]

    for cfg in configs_to_test:
        sig_name = cfg["signal"]
        sig_arr = signals[sig_name]

        # Split for validation
        tr_close, tr_high, tr_low = close[:mid], high[:mid], low[:mid]
        tr_sig = sig_arr[:mid]
        tr_vol = volume[:mid]

        oos_close, oos_high, oos_low = close[mid:], high[mid:], low[mid:]
        oos_sig = sig_arr[mid:]
        oos_vol = volume[mid:]

        mid_idx = n_dates // 2

        P(f"\n  {sig_name}/sl{cfg['sl']}/tp{cfg['tp']}/mh{cfg['mh']}/k{cfg['k']}/step{cfg['step']}/ts{cfg['tsma']}:")

        # Equal weighting (original)
        eq_r = run_strategy(oos_close, oos_high, oos_low, oos_sig,
                            cfg["sl"], cfg["tp"], cfg["mh"], cfg["k"], cfg["step"], cfg["tsma"],
                            volume=oos_vol)
        if "error" not in eq_r:
            P(f"    Equal weight:  wk={eq_r['weekly_mean_pct']:+.3f}% Sharpe={eq_r['sharpe']:.3f} "
              f"DD={eq_r['max_drawdown_pct']:.0f}% trades={eq_r['num_trades']} win={eq_r['win_rate_pct']:.0f}%")

        # Volatility targeting
        vol_r = run_with_vol_targeting(oos_close, oos_high, oos_low, oos_sig,
                                       cfg["sl"], cfg["tp"], cfg["mh"], cfg["k"], cfg["step"], cfg["tsma"],
                                       volume=oos_vol, vol_target_annual=0.25)
        if "error" not in vol_r:
            P(f"    Vol targeting: wk={vol_r['weekly_mean_pct']:+.3f}% Sharpe={vol_r['sharpe']:.3f} "
              f"DD={vol_r['max_drawdown_pct']:.0f}% trades={vol_r['num_trades']} win={vol_r['win_rate_pct']:.0f}%")

        # Parameter robustness: neighboring configs
        P(f"    Param robustness (OOS):")
        for sl_d in [-2, 0, +2]:
            for tp_d in [-5, 0, +5]:
                sl = cfg["sl"] + sl_d
                tp = cfg["tp"] + tp_d
                if sl <= 0 or tp <= 0:
                    continue
                r = run_strategy(oos_close, oos_high, oos_low, oos_sig,
                                 sl, tp, cfg["mh"], cfg["k"], cfg["step"], cfg["tsma"],
                                 volume=oos_vol)
                if "error" not in r:
                    tag = " *" if (sl_d == 0 and tp_d == 0) else "  "
                    P(f"      sl={sl}/tp={tp}: wk={r['weekly_mean_pct']:+.3f}% "
                      f"Sharpe={r['sharpe']:.3f} DD={r['max_drawdown_pct']:.0f}% trades={r['num_trades']}{tag}")

    # ================================================================
    # PART 3: Monthly return decomposition (best config)
    # ================================================================
    P(f"\n{'='*72}")
    P(f"MONTHLY RETURN DECOMPOSITION (best config OOS)")
    P(f"{'='*72}")

    best = configs_to_test[0]
    sig_arr = signals[best["signal"]]
    r = run_strategy(close[mid:], high[mid:], low[mid:], sig_arr[mid:],
                     best["sl"], best["tp"], best["mh"], best["k"], best["step"], best["tsma"],
                     volume=volume[mid:])

    if "error" not in r:
        # Compute monthly returns
        warmup = 22
        step = best["step"]
        step_size = 20
        all_dates = []
        all_returns = []
        n_oos = close[mid:].shape[0]

        for t in range(warmup, n_oos - best["mh"], step):
            scores = sig_arr[mid:][t, :].copy()
            valid = close[mid:][t, :] > 0
            scores = np.where(valid, scores, -np.inf)
            top_k = min(best["k"], int(np.sum(scores > -np.inf)))
            if top_k < best["k"]:
                continue
            picks = np.argpartition(-scores, top_k - 1)[:top_k]
            picks = picks[np.argsort(-scores[picks])]
            exit_r, ns, nt, nh = compute_exit_returns(
                close[mid:], high[mid:], low[mid:], t, picks, best["mh"],
                best["sl"], best["tp"]
            )
            net = np.mean(exit_r - COST_PER_ROUNDTRIP)
            all_dates.append(dates[mid + t])
            all_returns.append(net)

        if all_returns:
            wr = np.array(all_returns) * (5.0 / step)
            dr = pd.DataFrame({"date": all_dates, "ret": wr})
            dr["month"] = dr["date"].apply(lambda x: x.to_period("M"))
            monthly = dr.groupby("month")["ret"].agg(["mean", "std", "count"])
            P(f"\n  Monthly OOS performance:")
            for m, row in monthly.iterrows():
                P(f"  {m}: avg={row['mean']:+.3f}% std={row['std']:.3f}% n={int(row['count'])}")
            P(f"\n  Overall: avg_weekly={wr.mean()*100:.3f}% std={wr.std()*100:.3f}% "
              f"min={wr.min()*100:+.3f}% max={wr.max()*100:+.3f}%")
            P(f"  P(positive week)={(wr > 0).mean()*100:.0f}%")

    # ================================================================
    # SUMMARY
    # ================================================================
    P(f"\n{'='*72}")
    P(f"FINAL SUMMARY")
    P(f"{'='*72}")

    # Best overall config with vol targeting
    P(f"\n  Best configs (OOS, with stops):")
    for cfg in configs_to_test:
        sig_arr = signals[cfg["signal"]]
        mid_idx = n_dates // 2
        eq_r = run_strategy(close[mid_idx:], high[mid_idx:], low[mid_idx:],
                            sig_arr[mid_idx:], cfg["sl"], cfg["tp"], cfg["mh"],
                            cfg["k"], cfg["step"], cfg["tsma"],
                            volume=volume[mid_idx:])
        vol_r = run_with_vol_targeting(close[mid_idx:], high[mid_idx:], low[mid_idx:],
                                       sig_arr[mid_idx:], cfg["sl"], cfg["tp"], cfg["mh"],
                                       cfg["k"], cfg["step"], cfg["tsma"], volume=volume[mid_idx:],
                                       vol_target_annual=0.25)
        if "error" not in eq_r:
            P(f"\n  {cfg['signal']}/sl{cfg['sl']}/tp{cfg['tp']}/mh{cfg['mh']}/k{cfg['k']}/s{cfg['step']}/ts{cfg['tsma']}:")
            P(f"    Equal weight:     wk={eq_r['weekly_mean_pct']:+.3f}% Sharpe={eq_r['sharpe']:.3f} "
              f"DD={eq_r['max_drawdown_pct']:.0f}% win_week={eq_r['weekly_win_rate_pct']:.0f}%")
            P(f"    P(>=2%wk)={eq_r['weeks_ge_2pct_pct']}% P(>=5%wk)={eq_r['weeks_ge_5pct_pct']}%")
            if "error" not in vol_r:
                P(f"    Vol targeting:     wk={vol_r['weekly_mean_pct']:+.3f}% Sharpe={vol_r['sharpe']:.3f} "
                  f"DD={vol_r['max_drawdown_pct']:.0f}% win_week={vol_r['weekly_win_rate_pct']:.0f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
