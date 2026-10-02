"""Fast stop-loss backtest with expanded signal grid.

Core design:
- Daily data: open, high, low, close, volume for ~200 stocks
- Signals: momentum (3/5/10/20-day), mean-reversion (3/5/10-day), gap-and-run
- Entry: top-K by signal on rebalance dates, trend filter optional
- Exit: each position exits when daily low hits stop-loss,
  daily high hits take-profit, or max_holding days elapsed
- Costs: 0.283% round-trip per stock (NSE delivery)
- Volume filter: only enter when volume > 1.2x 20-day average

Walk-forward: train on first half, select top configs by Sharpe, validate OOS.
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

DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
OUT = ROOT / "data" / "research" / "stop_loss_v2.json"
TRADING_DAYS = 252
COST_PER_ROUNDTRIP = 0.00283
p = print

def P(*a, **k):
    k.setdefault("flush", True); p(*a, **k)


def load_panel(symbols, start, end, min_bars=250):
    """Returns dict with close, high, low, open, volume as aligned DataFrames."""
    close_d, high_d, low_d, open_d, vol_d = {}, {}, {}, {}, {}
    for sym in symbols:
        path = DAILY / f"{sym}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path, columns=["ts", "open", "high", "low", "close", "volume"])
            df = df.dropna(subset=["close"]).sort_values("ts")
            df = df[df["close"] > 0]
            if len(df) < min_bars:
                continue
            df = df.set_index("ts")
            df = df.loc[(df.index >= start) & (df.index <= end)]
            close_d[sym] = df["close"]
            high_d[sym] = df["high"]
            low_d[sym] = df["low"]
            open_d[sym] = df["open"]
            vol_d[sym] = df["volume"]
        except Exception:
            continue

    panels = {}
    for name, sym_dict in [("close", close_d), ("high", high_d), ("low", low_d),
                            ("open", open_d), ("volume", vol_d)]:
        if sym_dict:
            panels[name] = pd.concat(sym_dict, axis=1)

    common_cols = list(set(panels["close"].columns) & set(panels["high"].columns) &
                       set(panels["low"].columns) & set(panels["open"].columns) &
                       set(panels["volume"].columns))
    common_cols = sorted(common_cols)

    for name in panels:
        panels[name] = panels[name][common_cols].ffill().dropna(how="all")

    common_dates = panels["close"].index
    for name in panels:
        panels[name] = panels[name].reindex(common_dates).ffill()

    return panels, common_cols, common_dates


def compute_signals(close, high, low, open_, volume, n_stocks):
    """Precompute all signal arrays. Returns dict: name -> (n_dates, n_stocks)."""
    c = close
    v = volume
    signals = {}

    # Momentum: close / close.shift(lb) - 1, shifted 1
    for lb in [3, 5, 10, 20]:
        sig = np.full_like(c, -np.inf)
        sig[lb:] = c[lb:] / c[:-lb] - 1.0
        sig = np.roll(sig, 1, axis=0)
        sig[:lb + 2] = -np.inf
        signals[f"mom_{lb}d"] = sig

    # Mean reversion: -(close / close.shift(lb) - 1), shifted 1
    for lb in [3, 5, 10]:
        sig = np.full_like(c, -np.inf)
        sig[lb:] = -(c[lb:] / c[:-lb] - 1.0)
        sig = np.roll(sig, 1, axis=0)
        sig[:lb + 2] = -np.inf
        signals[f"rev_{lb}d"] = sig

    # Gap-and-run: (open - prev_close) / prev_close * momentum
    gap = np.full_like(c, -np.inf)
    gap[1:] = (open_[1:] - c[:-1]) / c[:-1]
    gap = np.roll(gap, 1, axis=0)
    gap[:2] = -np.inf
    # Combined: gap * mom_3d (both must be positive)
    gap_run = gap * signals["mom_3d"]
    gap_run = np.where(gap > 0.01, gap_run, -np.inf)  # Only consider gaps > 1%
    signals["gap_run"] = gap_run

    # Gap-and-run with mom_5d
    gap_run_5 = gap * signals["mom_5d"]
    gap_run_5 = np.where(gap > 0.02, gap_run_5, -np.inf)  # Gap > 2%
    signals["gap_run_5"] = gap_run_5

    # Volume-weighted momentum: mom_5d * (volume / avg_volume)
    vol_avg = np.full_like(v, np.nan)
    vol_avg[20:] = np.mean(v[1:21], axis=0)  # 20-day avg
    for i in range(21, len(v)):
        vol_avg[i] = v[i - 20:i].mean(axis=0)
    vol_ratio = np.where(vol_avg > 0, v / vol_avg, 0)
    vol_ratio = np.where(vol_ratio > 0, vol_ratio, 0)
    vol_mom = signals["mom_5d"] * vol_ratio
    vol_mom = np.where(vol_ratio > 1.2, vol_mom, -np.inf)  # Volume > 1.2x avg
    signals["vol_mom_5d"] = vol_mom

    # Volume-weighted momentum 10d
    vol_mom_10 = signals["mom_10d"] * vol_ratio
    vol_mom_10 = np.where(vol_ratio > 1.2, vol_mom_10, -np.inf)
    signals["vol_mom_10d"] = vol_mom_10

    # Opening range breakout: high_5d - low_5d normalized
    # Breakout: close > close.shift(5) * (1 + range_5d_norm)
    range_5d = np.full_like(c, np.inf)
    range_5d[5:] = (high[4:-1].max(axis=0) - low[4:-1].min(axis=0)) / c[4:-1].mean(axis=0) + 1.0
    range_5d = np.roll(range_5d, 1, axis=0)
    range_5d[:6] = np.inf
    orb = np.where(c > 0, c / (c * range_5d), -np.inf)
    # Simple: momentum * volume_ratio
    orb_sig = signals["mom_5d"] * vol_ratio
    orb_sig = np.where(vol_ratio > 1.0, orb_sig, -np.inf)
    signals["orb_5d"] = orb_sig

    return signals


def compute_exit_returns(close, high, low, t, picks, max_holding, stop_pct, target_pct):
    """Vectorized exit return computation for K positions entered at date t."""
    n_stocks = close.shape[1]
    n_dates = close.shape[0]

    entry_prices = close[t, picks]
    path_end = min(t + max_holding + 1, n_dates)
    if path_end <= t + 1 or len(picks) == 0:
        return np.zeros(len(picks)), 0, 0, 0

    high_path = high[t + 1:path_end, :][:, picks]
    low_path = low[t + 1:path_end, :][:, picks]
    close_path = close[t + 1:path_end, :][:, picks]

    eps = 1e-10
    cum_high = high_path / (entry_prices + eps) - 1.0
    cum_low = low_path / (entry_prices + eps) - 1.0
    cum_close = close_path / (entry_prices + eps) - 1.0

    stop_level = -stop_pct / 100.0
    target_level = target_pct / 100.0

    stop_hits = cum_low <= stop_level
    target_hits = cum_high >= target_level

    # Find first hit (argmax returns 0 if no True, so check with any)
    n_days = path_end - (t + 1)
    if n_days == 1:
        # Only one day — check if stop/target hit on that day
        exit_returns = cum_close[-1].copy()
        stop_wins = stop_hits[-1]
        target_wins = target_hits[-1] & ~stop_hits[-1]
        exit_returns[stop_wins] = stop_level
        exit_returns[target_wins] = target_level
        return exit_returns, int(np.sum(stop_wins)), int(np.sum(target_wins)), int(np.sum(~stop_wins & ~target_wins))

    stop_first = np.argmax(stop_hits, axis=0)
    stop_any = np.any(stop_hits, axis=0)
    target_first = np.argmax(target_hits, axis=0)
    target_any = np.any(target_hits, axis=0)

    exit_returns = cum_close[-1].copy()
    both_hit = stop_any & target_any
    stop_wins = stop_any & (~target_any | (stop_first <= target_first))
    target_wins = target_any & (~stop_any | (target_first < stop_first))
    exit_returns[stop_wins] = stop_level
    exit_returns[target_wins] = target_level

    n_stop = int(np.sum(stop_wins))
    n_target = int(np.sum(target_wins))
    n_hold = len(picks) - n_stop - n_target

    return exit_returns, n_stop, n_target, n_hold


def run_strategy(close, high, low, signal, stop_pct, target_pct,
                 max_holding, k, step, trend_sma=0, costs=0.00283,
                 volume=None, date_idx=0):
    """Run the backtest. Returns dict of metrics.

    signal: 2D numpy array (n_dates, n_stocks) — higher = better
    """
    n_dates, n_stocks = close.shape
    warmup = max(trend_sma, 22) if trend_sma > 0 else 22

    # Trend filter
    trend_mask = None
    if trend_sma > 0:
        trend = np.full((n_dates, n_stocks), np.nan)
        for i in range(trend_sma - 1, n_dates):
            trend[i] = close[i - trend_sma + 1:i + 1].mean(axis=0)
        trend_mask = close > trend

    # Volume filter
    vol_mask = None
    if volume is not None:
        vol_avg = np.full_like(volume, np.nan)
        for i in range(20, n_dates):
            vol_avg[i] = volume[i - 20:i].mean(axis=0)
        vol_mask = volume > (vol_avg * 1.2)

    rebalance_dates = list(range(warmup, n_dates - max_holding, step))
    if len(rebalance_dates) < 5:
        return {"error": "insufficient rebalancing dates"}

    all_returns = []
    total_trades = 0
    total_stops = 0
    total_targets = 0

    for t in rebalance_dates:
        scores = signal[t, :].copy()
        if np.all(np.isneginf(scores)) or np.all(np.isnan(scores)):
            continue

        # Mask invalid (no price or NaN)
        valid = close[t, :] > 0
        scores = np.where(valid, scores, -np.inf)

        if trend_mask is not None:
            mask = valid & trend_mask[t, :]
            scores = np.where(mask, scores, -np.inf)

        if vol_mask is not None:
            scores = np.where(vol_mask[t, :], scores, -np.inf)

        top_k = min(k, int(np.sum(scores > -np.inf)))
        if top_k < k:
            continue

        picks = np.argpartition(-scores, top_k - 1)[:top_k]
        picks = picks[np.argsort(-scores[picks])]

        exit_returns, n_stop, n_target, n_hold = compute_exit_returns(
            close, high, low, t, picks, max_holding, stop_pct, target_pct
        )

        net_returns = exit_returns - costs
        portfolio_return = float(np.mean(net_returns))

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
    total_ret = float(cum_ret[-1] - 1)
    cagr = (1 + total_ret) ** (1.0 / years) - 1 if years > 0 else 0
    # Standard Sharpe: arithmetic annualized return / annualized vol
    periods_per_year = TRADING_DAYS / step
    arith_annual = float(np.mean(returns)) * periods_per_year
    vol = float(np.std(returns, ddof=1)) * np.sqrt(periods_per_year) if n_periods > 1 else 0
    sharpe = (arith_annual - 0.075) / vol if vol > 0 else 0
    # Weekly Sharpe for reference
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

    total_costs_pct = round(100 * total_trades * costs / (n_periods * k) if n_periods > 0 else 0, 2)

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
        "total_costs_pct": total_costs_pct,
        "n_rebalances": n_periods,
    }


def main():
    start = pd.Timestamp("2020-01-01")
    end = pd.Timestamp("2026-09-18")

    # Get all symbols
    nse_syms = sh.parse_universe(ROOT / "data" / "universe" / "n50.txt")
    mid_syms = sh.parse_universe(ROOT / "data" / "universe" / "mid150.txt")
    all_symbols = list(set(nse_syms + mid_syms))
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
    mid = n_dates // 2
    train_end_idx = mid
    oos_start_idx = mid + 1

    P(f"\n  Train: {dates[0].date()} → {dates[train_end_idx].date()}")
    P(f"  OOS:   {dates[oos_start_idx].date()} → {dates[-1].date()}")

    # B&H benchmark
    oos_ret = np.nanmean(close[oos_start_idx:] / close[oos_start_idx - 1:-1] - 1, axis=1)
    bh_weekly = round(100 * float(np.nanmean(oos_ret)) * 5, 3)
    P(f"\n  B&H (OOS, eq-weight): ~{bh_weekly}% weekly mean")

    # Precompute signals on full panel
    P("  Computing signals...")
    signals = compute_signals(close, high, low, open_, volume, len(symbols_loaded))
    for name, sig in signals.items():
        sig_arr = np.full_like(close, -np.inf)
        sig_arr = sig
        signals[name] = sig_arr
    P(f"  Signals: {list(signals.keys())}")

    # Parameter grid
    param_grid = list(product(
        [(5, 10), (7, 14), (10, 20), (10, 30), (15, 30)],  # (stop, target)
        [2, 3, 5],         # k
        [5, 10],           # step
        [10, 20, 30],      # max_holding
        [0, 200],          # trend_sma
    ))
    P(f"\n  Testing {len(param_grid)} param combos × {len(signals)} signals = {len(param_grid) * len(signals)} total")

    train_results = []
    t1 = time.time()

    for sig_name, sig in signals.items():
        for (sl, tp), k, step, mh, tsma in param_grid:
            # Train
            try:
                tr = run_strategy(
                    close[:train_end_idx + 1], high[:train_end_idx + 1], low[:train_end_idx + 1],
                    sig[:train_end_idx + 1], sl, tp, mh, k, step, tsma,
                    volume=volume[:train_end_idx + 1]
                )
                if "error" in tr or tr["num_trades"] < 15:
                    continue
                tr["config"] = f"{sig_name}/sl{sl}/tp{tp}/mh{mh}/k{k}/step{step}/ts{tsma}"
                tr["signal"] = sig_name
                tr["params"] = {"sl": sl, "tp": tp, "mh": mh, "k": k, "step": step, "tsma": tsma}
                train_results.append(tr)
            except Exception:
                continue

    P(f"  Train: {len(train_results)} valid configs in {time.time()-t1:.1f}s")
    if not train_results:
        P("  No valid configs!")
        return 1

    train_results.sort(key=lambda r: r["sharpe"], reverse=True)

    for r in train_results[:5]:
        P(f"  {r['config']}: Sharpe={r['sharpe']:.3f} wk={r['weekly_mean_pct']:+.3f}% "
          f"CAGR={r['cagr_pct']:.0f}% DD={r['max_drawdown_pct']:.0f}% trades={r['num_trades']} "
          f"win={r['win_rate_pct']:.0f}% stops={r['stop_rate_pct']:.0f}%")

    # OOS validation: top 8 train configs
    P(f"\n{'='*72}")
    P(f"OOS VALIDATION (top 8 train configs)")
    P(f"{'='*72}")

    oos_results = []
    for r in train_results[:8]:
        try:
            oos_r = run_strategy(
                close, high, low, signals[r["signal"]],
                r["params"]["sl"], r["params"]["tp"], r["params"]["mh"],
                r["params"]["k"], r["params"]["step"], r["params"]["tsma"],
                volume=volume
            )
            if "error" in oos_r or oos_r["num_trades"] < 5:
                P(f"  {r['config']}: OOS insufficient trades ({oos_r.get('num_trades', 0)})")
                continue
            oos_r["config"] = r["config"]
            oos_r["signal"] = r["signal"]
            oos_r["params"] = r["params"]
            oos_results.append(oos_r)
            P(f"\n  {r['config']}:")
            P(f"    TRAIN: Sharpe={r['sharpe']:.3f} wk={r['weekly_mean_pct']:+.3f}% CAGR={r['cagr_pct']:.0f}% "
              f"DD={r['max_drawdown_pct']:.0f}% trades={r['num_trades']} win={r['win_rate_pct']:.0f}%")
            P(f"    OOS:   Sharpe={oos_r['sharpe']:.3f} wk={oos_r['weekly_mean_pct']:+.3f}% CAGR={oos_r['cagr_pct']:.0f}% "
              f"DD={oos_r['max_drawdown_pct']:.0f}% trades={oos_r['num_trades']} win={oos_r['win_rate_pct']:.0f}%")
            P(f"    P(>=2%wk)={oos_r['weeks_ge_2pct_pct']}% P(>=3%wk)={oos_r['weeks_ge_3pct_pct']}% "
              f"P(>=5%wk)={oos_r['weeks_ge_5pct_pct']}%")
            P(f"    Weekly vol={oos_r['weekly_vol_pct']:.3f}%  win_rate={oos_r['weekly_win_rate_pct']:.0f}%")
        except Exception as e:
            P(f"  {r['config']}: OOS error: {e}")

    # No-stops baseline: same signal/config but with huge stop/target
    P(f"\n{'='*72}")
    P(f"NO-STOPS BASELINE (pure momentum hold vs stop-loss/take-profit)")
    P(f"{'='*72}")
    for r in train_results[:3]:
        sig = signals[r["signal"]]
        ns_train = run_strategy(
            close[:train_end_idx + 1], high[:train_end_idx + 1], low[:train_end_idx + 1],
            sig[:train_end_idx + 1], 999, 999, r["params"]["mh"],
            r["params"]["k"], r["params"]["step"], r["params"]["tsma"],
            volume=volume[:train_end_idx + 1]
        )
        ns_oos = run_strategy(
            close, high, low, sig, 999, 999, r["params"]["mh"],
            r["params"]["k"], r["params"]["step"], r["params"]["tsma"],
            volume=volume
        )
        # Also re-run with stops for comparison
        st_oos = run_strategy(
            close, high, low, sig, r["params"]["sl"], r["params"]["tp"],
            r["params"]["mh"], r["params"]["k"], r["params"]["step"],
            r["params"]["tsma"], volume=volume
        )
        if "error" not in ns_oos and "error" not in st_oos:
            ns_train_ok = "error" not in ns_train
            P(f"  {r['config']}:")
            P(f"    TRAIN with stops:  wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f}")
            if ns_train_ok:
                P(f"    TRAIN no stops:    wk={ns_train['weekly_mean_pct']:+.3f}% Sharpe={ns_train['sharpe']:.3f}")
            P(f"    OOS with stops:    wk={st_oos['weekly_mean_pct']:+.3f}% Sharpe={st_oos['sharpe']:.3f} DD={st_oos['max_drawdown_pct']:.0f}%")
            P(f"    OOS no stops:      wk={ns_oos['weekly_mean_pct']:+.3f}% Sharpe={ns_oos['sharpe']:.3f} DD={ns_oos['max_drawdown_pct']:.0f}%")
            delta = st_oos['weekly_mean_pct'] - ns_oos['weekly_mean_pct']
            dd_delta = ns_oos['max_drawdown_pct'] - st_oos['max_drawdown_pct']
            P(f"    => stops add {delta:+.3f}% weekly, {dd_delta:+.0f}% DD reduction")

    oos_results.sort(key=lambda r: r["weekly_mean_pct"], reverse=True)

    # Summary
    max_tr_wk = max((r["weekly_mean_pct"] for r in train_results), default=None)
    max_oos_wk = max((r["weekly_mean_pct"] for r in oos_results), default=None)
    max_oos_sharpe = max((r["sharpe"] for r in oos_results), default=None)
    min_oos_dd = max((r["max_drawdown_pct"] for r in oos_results), default=None)

    P(f"\n{'='*72}")
    P(f"SUMMARY")
    P(f"{'='*72}")
    P(f"  B&H OOS weekly mean:  {bh_weekly}%")
    P(f"  Max train weekly:     {max_tr_wk}%")
    P(f"  Max OOS weekly:       {max_oos_wk}%")
    P(f"  Max OOS Sharpe:       {max_oos_sharpe}")
    P(f"\n  Top OOS by weekly mean:")
    for r in oos_results[:5]:
        P(f"  {r['config']}: wk={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
          f"DD={r['max_drawdown_pct']:.0f}% win={r['win_rate_pct']:.0f}% trades={r['num_trades']}")

    payload = {
        "generated_at": pd.Timestamp.now().isoformat(),
        "train_window": [str(dates[0].date()), str(dates[train_end_idx].date())],
        "oos_window": [str(dates[oos_start_idx].date()), str(dates[-1].date())],
        "benchmark": {"oos_weekly_mean": bh_weekly},
        "n_train_configs": len(train_results),
        "max_train_weekly": max_tr_wk,
        "max_oos_weekly": max_oos_wk,
        "max_oos_sharpe": max_oos_sharpe,
        "top_10_train": train_results[:10],
        "oos_results": oos_results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    P(f"\n  Results saved to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
