"""Recent-period validation of gap-and-run strategy + expanded intraday backtest.

1. Daily gap-and-run on NIFTY50+Midcap150, 2025-01-01 → 2026-09-18
   (validates the strategy in the most recent market regime)
2. Intraday backtest on combined iifl_1min + iifl_1m data
"""
import sys, json, traceback
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

p = print
def P(*a, **k):
    k.setdefault("flush", True)
    p(*a, **k)

# ------------------------------------------------------------------ daily

DAILY = ROOT / "data" / "iifl_daily" / "NSEEQ"
TRADING_DAYS = 252
COST = 0.00283


def load_symbol(sym, start, end):
    path = DAILY / f"{sym}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path, columns=["ts","open","high","low","close","volume"])
        df = df.dropna(subset=["close"]).sort_values("ts")
        df = df[df["close"] > 0]
        df = df[(df["ts"] >= start) & (df["ts"] <= end)].set_index("ts")
        if len(df) < 50:
            return None
        return df
    except Exception:
        return None


def build_regime(nifty_close, window):
    n = len(nifty_close)
    filt = np.zeros(n, dtype=bool)
    for i in range(window - 1, n):
        sma = np.nanmean(nifty_close[i - window + 1:i + 1])
        filt[i] = nifty_close[i] > sma if not np.isnan(sma) else False
    return filt


def load_panel(symbols, start, end, min_bars=250):
    close_l, high_l, low_l, vol_l = {}, {}, {}, {}
    for sym in symbols:
        df = load_symbol(sym, start, end)
        if df is None or len(df) < min_bars:
            continue
        close_l[sym] = df["close"]
        high_l[sym] = df["high"]
        low_l[sym] = df["low"]
        vol_l[sym] = df["volume"]
    if not close_l:
        return None, None, None, None, []
    common = close_l[list(close_l.keys())[0]].index
    for s in close_l:
        common = common.intersection(close_l[s].index)
    common = sorted(common)
    close = pd.DataFrame({s: close_l[s].reindex(common).to_numpy() for s in close_l}, index=common)
    high = pd.DataFrame({s: high_l[s].reindex(common).to_numpy() for s in close_l}, index=common)
    low = pd.DataFrame({s: low_l[s].reindex(common).to_numpy() for s in close_l}, index=common)
    vol = pd.DataFrame({s: vol_l[s].reindex(common).to_numpy() for s in close_l}, index=common)
    return close, high, low, vol, list(close.columns)


def run_strategy(close, high, low, signal, sl, tp, mh, k, step, costs,
                 regime_filter=None, vol_target=0.20, vol_window=20):
    n_dates, n_stocks = close.shape
    warmup = max(mh + 22, 22)
    step = step
    rb_dates = list(range(warmup, n_dates - max_holding_to_idx(mh), step))
    # Simpler: rb_dates = range(warmup, n_dates - mh, step)
    rb_dates = list(range(warmup, n_dates - mh, step))
    if len(rb_dates) < 3:
        return {"error": "insufficient dates", "n_dates": n_dates}

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

        entry = close[t, picks]
        path_end = min(t + mh + 1, close.shape[0])
        if path_end <= t + 1:
            continue
        hi = high[t+1:path_end][:, picks]
        lo = low[t+1:path_end][:, picks]
        cl = close[t+1:path_end][:, picks]

        cum_hi = hi / (entry + 1e-10) - 1.0
        cum_lo = lo / (entry + 1e-10) - 1.0
        stop_lvl = -sl / 100.0
        tgt_lvl = tp / 100.0

        stop_hits = cum_lo <= stop_lvl
        target_hits = cum_hi >= tgt_lvl
        n_days = path_end - (t + 1)

        exits = cl[-1] / (entry + 1e-10) - 1.0
        if n_days > 1:
            sf = np.argmax(stop_hits, axis=0)
            sa = np.any(stop_hits, axis=0)
            tf = np.argmax(target_hits, axis=0)
            ta = np.any(target_hits, axis=0)
            sw = sa & (~ta | (sf <= tf))
            tw = ta & (~sa | (tf < sf))
            exits[sw] = stop_lvl
            exits[tw] = tgt_lvl
            n_stop = int(np.sum(sw))
            n_target = int(np.sum(tw))
        else:
            sw = stop_hits[-1]
            tw = target_hits[-1] & ~sw
            exits[sw] = stop_lvl
            exits[tw] = tgt_lvl
            n_stop = int(np.sum(sw))
            n_target = int(np.sum(tw))

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

    if len(all_ret) < 3:
        return {"error": f"insufficient trades ({len(all_ret)})"}

    returns = np.array(all_ret)
    n_p = len(returns)
    wf = 5.0 / step
    weekly_rets = returns * wf
    cum = np.cumprod(1 + weekly_rets)
    total_ret = float(cum[-1] - 1)
    years = n_p * step / TRADING_DAYS
    cagr = (1 + total_ret) ** (1.0 / years) - 1 if years > 0 else 0
    ppy = TRADING_DAYS / step
    arith_annual = float(np.mean(returns)) * ppy
    vol = float(np.std(returns, ddof=1)) * np.sqrt(ppy) if n_p > 1 else 0
    sharpe = (arith_annual - 0.075) / vol if vol > 0 else 0
    rmax = np.maximum.accumulate(cum)
    max_dd = float(np.min(cum / rmax - 1))

    return {
        "weekly_mean_pct": round(100 * np.mean(weekly_rets), 3),
        "weekly_vol_pct": round(100 * np.std(weekly_rets, ddof=1), 3) if len(weekly_rets) > 1 else 0,
        "sharpe": round(sharpe, 3),
        "cagr_pct": round(100 * cagr, 2),
        "total_return_pct": round(100 * total_ret, 2),
        "max_drawdown_pct": round(100 * max_dd, 2),
        "weekly_win_rate_pct": round(100 * np.mean(weekly_rets > 0), 1),
        "weeks_ge_2pct_pct": round(100 * np.mean(weekly_rets >= 0.02), 1),
        "weeks_ge_3pct_pct": round(100 * np.mean(weekly_rets >= 0.03), 1),
        "weeks_ge_5pct_pct": round(100 * np.mean(weekly_rets >= 0.05), 1),
        "num_trades": total_trades,
        "win_rate_pct": round(100 * total_targets / max(total_trades, 1), 1),
        "stop_rate_pct": round(100 * total_stops / max(total_trades, 1), 1),
        "n_rebalances": n_p,
        "min_weekly_pct": round(100 * np.min(weekly_rets), 3),
        "max_weekly_pct": round(100 * np.max(weekly_rets), 3),
        "median_weekly_pct": round(100 * np.median(weekly_rets), 3),
    }


def max_holding_to_idx(mh):
    return int(mh * 252 / 365)


def compute_gap_vol_signal(close, high, low, open_, vol):
    """gap_vol signal: gap >= 1.5%, vol_ratio > 1.0, mom > 0."""
    c = close; o = open_; v = vol
    gap = np.full_like(c, -np.inf)
    gap[1:] = (o[1:] - c[:-1]) / c[:-1]
    gap = np.roll(gap, 1, axis=0); gap[:2] = -np.inf

    mom_5d = np.full_like(c, -np.inf)
    mom_5d[5:] = c[5:] / c[:-5] - 1.0
    mom_5d = np.roll(mom_5d, 1, axis=0); mom_5d[:6] = -np.inf

    vol_avg = np.full_like(v, np.nan)
    for i in range(20, len(v)):
        vol_avg[i] = v[i - 20:i].mean(axis=0)
    vr = np.where(vol_avg > 0, v / vol_avg, 0)

    signal = np.where((gap > 0.015) & (vr > 1.0) & (mom_5d > 0),
                      gap * mom_5d * (1 + np.log1p(vr)), -np.inf)
    return signal


# ------------------------------------------------------------------ intraday

def load_1min_data():
    """Load 1-min parquet from both iifl_1min and iifl_1m directories."""
    df_dict = {}
    for cache in [ROOT / "data" / "iifl_1min", ROOT / "data" / "iifl_1m"]:
        if not cache.exists():
            continue
        for f in sorted(cache.glob("*.parquet")):
            sym = f.stem
            try:
                df = pd.read_parquet(f)
                if len(df) < 500:
                    continue
                if sym not in df_dict or (isinstance(df_dict[sym]["ts"].iloc[0], pd.Timestamp) and
                                          df["ts"].iloc[0] < df_dict[sym]["ts"].iloc[0]):
                    df_dict[sym] = df
                elif df_dict[sym]["ts"].iloc[-1] < df["ts"].iloc[-1]:
                    # Use the one with more recent data
                    pass
            except Exception:
                continue
    return df_dict


def intraday_gap_run(df_dict, sl_pct=1.0, tp_pct=2.0, k=3, max_hold_bars=60,
                     costs=0.001, min_gap=0.5, min_vol_ratio=1.0):
    """Intraday gap-and-run backtest on 1-minute bars."""
    daily = {}
    for sym, df in df_dict.items():
        if len(df) < 100:
            continue
        d = df.copy()
        d["date"] = d["ts"].dt.normalize()
        agg = d.groupby("date").agg(
            open=("open", "first"), high=("high", "max"),
            low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
        )
        daily[sym] = agg

    if not daily:
        return {"error": "no data"}

    all_dates = sorted(set(idx for d in daily.values() for idx in d.index))
    close_daily = pd.DataFrame({s: d["close"] for s, d in daily.items()})
    open_daily = pd.DataFrame({s: d["open"] for s, d in daily.items()})
    vol_daily = pd.DataFrame({s: d["volume"] for s, d in daily.items()})
    for s in close_daily:
        close_daily[s] = close_daily[s].reindex(all_dates).ffill()
        open_daily[s] = open_daily[s].reindex(all_dates).ffill()
        vol_daily[s] = vol_daily[s].reindex(all_dates).ffill()

    gap = (open_daily / close_daily.shift(1) - 1.0)
    vol_avg = vol_daily.rolling(20).mean()
    vol_ratio = vol_daily / vol_avg.replace(0, np.nan)
    mom_5d = close_daily / close_daily.shift(5) - 1.0

    # Regime filter (50-day SMA on NIFTYBEES)
    regime_ok = None
    if "NIFTYBEES" in df_dict:
        n_close = close_daily["NIFTYBEES"]
        n_sma = n_close.rolling(50).mean()
        regime_ok = n_close > n_sma
        for col in ["NIFTYBEES"]:
            close_daily = close_daily.drop(columns=col, errors="ignore")
            open_daily = open_daily.drop(columns=col, errors="ignore")
            vol_ratio = vol_ratio.drop(columns=col, errors="ignore")
            gap = gap.drop(columns=col, errors="ignore")
            mom_5d = mom_5d.drop(columns=col, errors="ignore")

    signal_mask = (gap >= min_gap / 100.0) & (vol_ratio >= min_vol_ratio) & (mom_5d > 0)
    scores = gap * mom_5d * (1 + np.log1p(vol_ratio))

    all_trades = []
    warmup = 50
    for i in range(warmup, len(all_dates)):
        date = all_dates[i]
        if regime_ok is not None and not regime_ok.iloc[i]:
            continue
        day_scores = scores.iloc[i]
        day_mask = signal_mask.iloc[i] & close_daily.iloc[i].notna()
        n_valid = int(day_mask.sum())
        if n_valid < k:
            continue
        picks = day_scores[day_mask].nlargest(k).index.tolist()
        for sym in picks:
            entry_price = open_daily.iloc[i][sym]
            if np.isnan(entry_price) or entry_price <= 0:
                continue
            intraday = df_dict.get(sym)
            if intraday is None or len(intraday) < 2:
                continue
            day_data = intraday[intraday["ts"].dt.normalize() == date]
            if len(day_data) < 2:
                continue
            bars = day_data.iloc[1:].reset_index(drop=True)
            exit_price = None
            exit_reason = "max_hold"
            for j, (_, row) in enumerate(bars.iterrows()):
                if row["low"] <= entry_price * (1 - sl_pct / 100):
                    exit_price = entry_price * (1 - sl_pct / 100)
                    exit_reason = "stop"
                    break
                if row["high"] >= entry_price * (1 + tp_pct / 100):
                    exit_price = entry_price * (1 + tp_pct / 100)
                    exit_reason = "target"
                    break
                if j + 1 >= max_hold_bars:
                    exit_price = row["close"]
                    exit_reason = "max_hold"
                    break
            if exit_price is None:
                exit_price = bars.iloc[-1]["close"] if len(bars) > 0 else entry_price

            ret = (exit_price - entry_price) / entry_price - costs
            all_trades.append({
                "date": date.strftime("%Y-%m-%d"),
                "symbol": sym, "entry": entry_price, "exit": exit_price,
                "ret": ret, "reason": exit_reason,
            })

    if not all_trades:
        return {"error": "no trades"}

    trades = pd.DataFrame(all_trades)
    if "weekly_mean_pct" in locals():
        pass
    daily_ret = trades.groupby("date")["ret"].mean()
    rets = daily_ret.values
    if len(rets) < 3:
        return {"error": f"only {len(rets)} rebalances"}

    # Weekly compounding (5 trading days per week)
    weekly = pd.Series(rets).groupby(np.arange(len(rets)) // 5 * 5).mean()
    weekly_rets = weekly.values
    cum = np.cumprod(1 + weekly_rets)
    total_ret = float(cum[-1] - 1)
    n = len(weekly_rets)
    ann_ret = (1 + total_ret) ** (52 / n) - 1 if n > 0 else 0
    vol = float(np.std(weekly_rets, ddof=1)) * np.sqrt(52 / n) if n > 1 else 0
    sharpe = (ann_ret - 0.075) / vol if vol > 0 else 0
    rmax = np.maximum.accumulate(cum)
    max_dd = float(np.min(cum / rmax - 1))

    return {
        "avg_trade_pct": round(100 * float(np.mean(trades["ret"])), 3),
        "total_return_pct": round(100 * total_ret, 2),
        "annual_return_pct": round(100 * ann_ret, 1),
        "annual_vol_pct": round(100 * vol, 1),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(100 * max_dd, 1),
        "win_rate_pct": round(100 * float((trades["ret"] > 0).mean()), 1),
        "n_trades": len(trades),
        "n_rebalances": n,
        "stop_rate_pct": round(100 * float((trades["reason"] == "stop").mean()), 1),
        "target_rate_pct": round(100 * float((trades["reason"] == "target").mean()), 1),
        "hold_rate_pct": round(100 * float((trades["reason"] == "max_hold").mean()), 1),
        "weekly_mean_pct": round(100 * float(np.mean(weekly_rets)), 3),
        "weekly_median_pct": round(100 * float(np.median(weekly_rets)), 3),
        "min_weekly_pct": round(100 * float(np.min(weekly_rets)), 3),
        "max_weekly_pct": round(100 * float(np.max(weekly_rets)), 3),
    }


# ------------------------------------------------------------------ main
def main():
    start = pd.Timestamp("2025-01-01")
    end = pd.Timestamp("2026-09-18")

    # Universe
    uni_map = {}
    for f in [ROOT / "data" / "universe" / "n50.txt",
              ROOT / "data" / "universe" / "mid150.txt"]:
        if f.exists():
            content = f.read_text().strip()
            syms = [s.strip() for s in content.replace("\n", ",").split(",") if s.strip()]
            uni_map[f.stem] = syms
    all_symbols = list(dict.fromkeys(
        uni_map.get("n50", []) + uni_map.get("mid150", [])
    ))
    # Add NIFTYBEES for regime filtering (ETF tracks NIFTY 50)
    if "NIFTYBEES" not in all_symbols:
        all_symbols.append("NIFTYBEES")
    P(f"Universe: {len(all_symbols)} symbols ({len(uni_map.get('n50',[]))} NIFTY50 + {len(uni_map.get('mid150',[]))} Midcap150 + NIFTYBEES for regime)")

    # Load daily panel (close, high, low, vol, open)
    close_arr, high_arr, low_arr, vol_arr, syms = load_panel(all_symbols, start, end, min_bars=50)
    if close_arr is None:
        P("ERROR: No data loaded")
        return 1
    dates = close_arr.index
    P(f"Loaded: {len(syms)} stocks, {len(dates)} dates ({dates[0].date()} → {dates[-1].date()})")

    # Reload with open prices included
    close_d, high_d, low_d, vol_d, open_d = {}, {}, {}, {}, {}
    for sym in syms:
        df = load_symbol(sym, start, end)
        if df is None:
            continue
        idx = df.index.intersection(dates)
        close_d[sym] = df.loc[idx, "close"].to_numpy(dtype=np.float64)
        high_d[sym] = df.loc[idx, "high"].to_numpy(dtype=np.float64)
        low_d[sym] = df.loc[idx, "low"].to_numpy(dtype=np.float64)
        vol_d[sym] = df.loc[idx, "volume"].to_numpy(dtype=np.float64)
        open_d[sym] = df.loc[idx, "open"].to_numpy(dtype=np.float64)
    syms2 = list(close_d.keys())
    close = np.column_stack([close_d[s] for s in syms2])
    high = np.column_stack([high_d[s] for s in syms2])
    low = np.column_stack([low_d[s] for s in syms2])
    vol = np.column_stack([vol_d[s] for s in syms2])
    open_ = np.column_stack([open_d[s] for s in syms2])
    syms = syms2

    # NIFTY regime
    nidx = syms.index("NIFTYBEES") if "NIFTYBEES" in syms else -1
    if nidx >= 0:
        n_close = close[:, nidx]
        regime_200 = build_regime(n_close, 200)
        regime_50 = build_regime(n_close, 50)
        regime_dual = regime_50 & regime_200
        P(f"NIFTYBEES found at index {nidx}. Regime: dual={regime_dual.sum()} days, "
          f"200d={regime_200.sum()} days")
    else:
        regime_200 = None
        regime_50 = None
        regime_dual = None
        P("WARNING: NIFTYBEES not loaded — regime filters disabled")

    # Compute gap_vol signal
    gap = np.full_like(close, -np.inf)
    gap[1:] = (open_[1:] - close[:-1]) / close[:-1]
    gap = np.roll(gap, 1, axis=0); gap[:2] = -np.inf

    mom_5d = np.full_like(close, -np.inf)
    mom_5d[5:] = close[5:] / close[:-5] - 1.0
    mom_5d = np.roll(mom_5d, 1, axis=0); mom_5d[:6] = -np.inf

    vol_avg = np.full_like(vol, np.nan)
    for i in range(20, len(vol)):
        vol_avg[i] = vol[i - 20:i].mean(axis=0)
    vr = np.where(vol_avg > 0, vol / vol_avg, 0)

    gap_vol = np.where((gap > 0.015) & (vr > 1.0) & (mom_5d > 0),
                       gap * mom_5d * (1 + np.log1p(vr)), -np.inf)

    P(f"\n{'='*80}")
    P("RECENT-PERIOD DAILY BACKTEST (2025-01-01 → 2026-09-18)")
    P(f"{'='*80}")

    configs = [
        ("gap_vol", "dual", 15, 30, 30, 3, 5, 0.20),
        ("gap_vol", "dual", 10, 20, 20, 3, 5, 0.20),
        ("gap_vol", "200d", 15, 30, 30, 3, 5, 0.20),
        ("gap_vol", "none", 15, 30, 30, 3, 5, 0.20),
    ]

    for name, regime, sl, tp, mh, k, step, vt in configs:
        reg = {"dual": regime_dual, "200d": regime_200, "none": None}.get(regime)
        try:
            r = run_strategy(close, high, low, gap_vol, sl, tp, mh, k, step,
                            COST, regime_filter=reg, vol_target=vt)
            if "error" in r:
                P(f"  gap_vol/{regime}/sl{sl}/tp{tp}/mh{mh}/k{k}/step{step}/vt{vt}: {r['error']}")
            else:
                target_met = "✓ TARGET" if 2 <= r["weekly_mean_pct"] <= 5 else "✗ OUT"
                P(f"  gap_vol/{regime}/sl{sl}/tp{tp}/mh{mh}/k{k}/step{step}/vt{vt}:")
                P(f"    weekly={r['weekly_mean_pct']:+.3f}%  Sharpe={r['sharpe']:.3f}  "
                  f"DD={r['max_drawdown_pct']:.1f}%  total={r['total_return_pct']:+.1f}% "
                  f"rb={r['n_rebalances']} trades={r['num_trades']} "
                  f"stop={r['stop_rate_pct']:.0f}% tgt={r['win_rate_pct']:.0f}% "
                  f"P(>=2%)={r['weeks_ge_2pct_pct']}% P(>=5%)={r['weeks_ge_5pct_pct']}% "
                  f"win={r['weekly_win_rate_pct']}%  {target_met}")
        except Exception as e:
            P(f"  gap_vol/{regime}/sl{sl}/tp{tp}/mh{mh}/k{k}/step{step}: ERROR — {e}")
            traceback.print_exc()

    # ------------------------------------------------------------------ intraday
    P(f"\n{'='*80}")
    P("INTRADAY 1-MIN BACKTEST (combined data)")
    P(f"{'='*80}")

    df_dict = load_1min_data()
    P(f"Loaded {len(df_dict)} 1-min symbols")
    for s, df in sorted(df_dict.items()):
        if len(df) > 0:
            P(f"  {s}: {len(df)} bars ({df['ts'].iloc[0]} → {df['ts'].iloc[-1]})")

    if len(df_dict) < 2:
        P("Not enough 1-min data for backtest")
    else:
        # Test a few key configs
        id_configs = [
            (0.5, 1.0, 3, 60, "tight_1gap_k3_1h"),
            (1.0, 2.0, 3, 60, "tight_1pct_gap_k3_1h"),
            (1.0, 2.0, 5, 30, "tight_1pct_gap_k5_30m"),
            (1.5, 3.0, 3, 60, "med_1gap_k3_1h_lowthresh"),
            (1.5, 3.0, 5, 30, "med_1gap_k5_30m"),
        ]
        for sl, tp, k, hold, label in id_configs:
            try:
                r = intraday_gap_run(df_dict, sl_pct=sl, tp_pct=tp, k=k,
                                     max_hold_bars=hold, min_gap=0.5)
                if "error" in r:
                    P(f"  {label}: {r['error']}")
                else:
                    P(f"  {label}: weekly={r['weekly_mean_pct']:+.3f}% Sharpe={r['sharpe']:.3f} "
                      f"DD={r['max_drawdown_pct']:.1f}% total={r['total_return_pct']:+.1f}% "
                      f"trades={r['n_trades']} rb={r['n_rebalances']} "
                      f"stop={r['stop_rate_pct']:.0f}% tgt={r['target_rate_pct']:.0f}%")
            except Exception as e:
                P(f"  {label}: ERROR — {e}")

    P("\nDone.")


if __name__ == "__main__":
    raise SystemExit(main())
