"""Fetch 1-minute data from IIFL and run intraday gap-and-run backtest."""
import sys, time, traceback
from pathlib import Path
from datetime import datetime, timedelta

import json
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atr.brokers.iifl.auth import SessionStore
from atr.brokers.iifl.client import IiflClient, _fmt_date
from atr.brokers.iifl.contracts import InstrumentMaster
from atr.brokers.iifl.feeds import _candles, _candle_row
from atr.config.settings import get_settings

p = print
def P(*a, **kw): kw.setdefault("flush", True); p(*a, **kw)


def fetch_1min(client, master, sym, from_dt, to_dt, exchange="NSEEQ",
               cache_dir="data/iifl_1min", max_days=45):
    cache = ROOT / cache_dir
    cache.mkdir(parents=True, exist_ok=True)

    inst = master.search(sym, exchange=exchange)
    if inst.empty:
        P(f"  {sym}: NOT FOUND"); return pd.DataFrame()
    conid = str(inst.iloc[0]["conid"])

    rows = []
    cur = from_dt
    chunks = 0
    while cur < to_dt:
        end = min(cur + timedelta(days=max_days), to_dt)
        payload = client.historical_data(
            exchange=exchange, instrument_id=conid,
            interval="1m", from_date=_fmt_date(cur), to_date=_fmt_date(end),
        )
        candles = _candles(payload)
        for raw in candles:
            row = _candle_row(raw)
            if row:
                rows.append({
                    "ts": pd.to_datetime(row["ts"]),
                    "symbol": sym,
                    "open": float(row["open"] or 0),
                    "high": float(row["high"] or 0),
                    "low": float(row["low"] or 0),
                    "close": float(row["close"] or 0),
                    "volume": float(row["volume"] or 0),
                })
        chunks += 1
        cur = end + timedelta(days=1)
        time.sleep(0.1)

    if not rows:
        P(f"  {sym}: no data"); return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    df.to_parquet(cache / f"{sym}.parquet", index=False)
    P(f"  {sym}: {len(df)} bars ({chunks} chunks) saved")
    return df


def backtest_1min(df_dict, sl_pct=1.5, tp_pct=3.0, k=5, max_hold_bars=120,
                  costs=0.001, min_gap=0.5, min_vol_ratio=1.0):
    """Intraday gap-and-run backtest on 1-minute bars."""
    # Extract daily bars from 1-min data
    daily = {}
    for sym, df in df_dict.items():
        d = df.copy()
        d["date"] = d["ts"].dt.normalize()
        agg = d.groupby("date").agg(
            open=("open", "first"), high=("high", "max"),
            low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
        )
        daily[sym] = agg

    all_dates = sorted(set(idx for d in daily.values() for idx in d.index))

    # Build daily close panel
    close_daily = pd.DataFrame({s: d["close"] for s, d in daily.items()})
    for s in close_daily:
        close_daily[s] = close_daily[s].reindex(all_dates).ffill()
    open_daily = pd.DataFrame({s: d["open"] for s, d in daily.items()})
    for s in open_daily:
        open_daily[s] = open_daily[s].reindex(all_dates).ffill()
    vol_daily = pd.DataFrame({s: d["volume"] for s, d in daily.items()})
    for s in vol_daily:
        vol_daily[s] = vol_daily[s].reindex(all_dates).ffill()

    gap = (open_daily / close_daily.shift(1) - 1.0)
    vol_avg = vol_daily.rolling(20).mean()
    vol_ratio = vol_daily / vol_avg.replace(0, np.nan)
    mom_5d = close_daily / close_daily.shift(5) - 1.0

    # NIFTY regime filter — use 50-day SMA (more responsive for intraday)
    regime_ok = None
    if "NIFTYBEES" in df_dict:
        n_close = pd.Series(
            {d: close_daily.loc[d, "NIFTYBEES"] if "NIFTYBEES" in close_daily else np.nan
             for d in all_dates}
        )
        n_sma = n_close.rolling(50).mean()
        regime_ok = n_close > n_sma
        close_daily = close_daily.drop(columns=["NIFTYBEES"])
        open_daily = open_daily.drop(columns=["NIFTYBEES"])
        vol_ratio = vol_ratio.drop(columns=["NIFTYBEES"])
        gap = gap.drop(columns=["NIFTYBEES"])
        mom_5d = mom_5d.drop(columns=["NIFTYBEES"])

    # Signal: gap >= min_gap% AND vol_ratio >= min_vol_ratio AND mom > 0
    signal_mask = (gap >= min_gap / 100.0) & (vol_ratio >= min_vol_ratio) & (mom_5d > 0)
    scores = gap * mom_5d * (1 + np.log1p(vol_ratio))

    all_trades = []
    warmup = 50  # need 50 days for 50-day SMA + vol ratio
    for i in range(warmup, len(all_dates)):
        date = all_dates[i]

        # Apply regime filter only if it's available
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

            intraday = df_dict[sym]
            day_data = intraday[intraday["ts"].dt.normalize() == date]
            if len(day_data) < 2:
                continue

            bars = day_data.iloc[1:].reset_index(drop=True)
            exit_price = None
            exit_reason = "max_hold"
            for j, row in bars.iterrows():
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
            else:
                exit_price = bars.iloc[-1]["close"] if len(bars) > 0 else entry_price

            ret = (exit_price - entry_price) / entry_price - costs
            all_trades.append({
                "date": date.strftime("%Y-%m-%d"),
                "symbol": sym, "entry": entry_price, "exit": exit_price,
                "ret": ret, "reason": exit_reason,
                "gap": gap.iloc[i][sym] if sym in gap.columns else 0,
            })

    if not all_trades:
        return {"error": "no trades", "detail": "no gap-and-run signals found in period"}

    trades = pd.DataFrame(all_trades)
    daily_ret = trades.groupby("date")["ret"].mean()
    rets = daily_ret.values

    if len(rets) < 3:
        return {"error": f"only {len(rets)} rebalances"}

    cum = np.cumprod(1 + rets)
    total_ret = float(cum[-1] - 1)
    n = len(rets)
    ann_ret = (1 + total_ret) ** (252 / n) - 1
    vol = float(np.std(rets, ddof=1)) * np.sqrt(252 / n) if n > 1 else 0
    sharpe = (ann_ret - 0.075) / vol if vol > 0 else 0
    rmax = np.maximum.accumulate(cum)
    max_dd = float(np.min(cum / rmax - 1))

    stop_rate = float((trades["reason"] == "stop").mean())
    target_rate = float((trades["reason"] == "target").mean())
    hold_rate = float((trades["reason"] == "max_hold").mean())

    return {
        "avg_trade_pct": round(100 * float(np.mean(trades["ret"])), 3),
        "total_return_pct": round(100 * total_ret, 2),
        "annual_return_pct": round(100 * ann_ret, 1),
        "annual_vol_pct": round(100 * vol, 1),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(100 * max_dd, 2),
        "win_rate_pct": round(100 * float((trades["ret"] > 0).mean()), 1),
        "n_trades": len(trades),
        "n_rebalances": n,
        "stop_rate_pct": round(100 * stop_rate, 1),
        "target_rate_pct": round(100 * target_rate, 1),
        "hold_rate_pct": round(100 * hold_rate, 1),
        "avg_gap_pct": round(100 * float(trades["gap"].mean()), 2),
        "avg_entry": round(float(trades["entry"].mean()), 2),
        "best_day": round(100 * float(daily_ret.max()), 2),
        "worst_day": round(100 * float(daily_ret.min()), 2),
    }


def main():
    settings = get_settings()
    store = SessionStore(settings.iifl_session_cache)
    session = store.load()
    if not session:
        P("NO SESSION")
        return 1

    client = IiflClient(
        app_key=settings.iifl_app_key,
        app_secret=settings.iifl_app_secret,
        session_store=store, timeout=30,
    )
    client.set_session(session)

    P(f"Session: {session.client_id}, expires {session.expires_at}")

    # Load existing 1-min data, fetch if missing
    cache = ROOT / "data" / "iifl_1min"
    cached_files = sorted(cache.glob("*.parquet"))
    need_fetch = [f.stem for f in cached_files if f.stem != "TATAMOTORS"]
    P(f"Cached symbols: {need_fetch}")

    from_dt = datetime(2025, 9, 1)
    to_dt = datetime(2026, 9, 15)

    # Only fetch if API session is valid and data is missing
    master = None
    session_still_valid = session.is_valid()

    df_dict = {}
    for sym in need_fetch:
        fpath = cache / f"{sym}.parquet"
        if fpath.exists():
            df_dict[sym] = pd.read_parquet(fpath)
            P(f"  {sym}: loaded {len(df_dict[sym])} bars from cache")
        else:
            if session_still_valid:
                P(f"  {sym}: fetching...")
                if master is None:
                    from atr.brokers.iifl.contracts import InstrumentMaster
                    master = InstrumentMaster(client)
                    P("Syncing instrument master...")
                    master.sync(["NSEEQ"])
                df_dict[sym] = fetch_1min(client, master, sym, from_dt, to_dt)

    client.close()

    # Verify data
    P(f"\nData: {len(df_dict)} symbols")
    for sym, df in sorted(df_dict.items()):
        if len(df) > 0:
            P(f"  {sym}: {len(df)} bars ({df.ts.min()} → {df.ts.max()})")

    # Remove empty DataFrames
    empty = [s for s, d in df_dict.items() if len(d) == 0]
    for s in empty:
        del df_dict[s]
        P(f"  Removed empty: {s}")
    P(f"  Active symbols: {len(df_dict)}")

    # Run backtests
    P(f"\n{'='*80}")
    P("INTRADAY GAP-AND-RUN BACKTEST (1-minute data)")
    P(f"{'='*75}")

    configs = [
        # sl, tp, k, hold_bars, gap_thresh, label
        (0.3, 0.8, 3, 30, 0.3, "very_tight_1gap_k3_30m"),
        (0.3, 0.8, 5, 30, 0.3, "very_tight_1gap_k5_30m"),
        (0.3, 0.8, 10, 30, 0.3, "very_tight_1gap_k10_30m"),
        (0.5, 1.0, 3, 30, 0.3, "tight_1gap_k3_30m"),
        (0.5, 1.0, 5, 30, 0.3, "tight_1gap_k5_30m"),
        (0.5, 1.0, 10, 30, 0.3, "tight_1gap_k10_30m"),
        (0.5, 1.0, 15, 30, 0.3, "tight_1gap_k15_30m"),
        (0.5, 1.0, 5, 60, 0.3, "tight_1gap_k5_1h_lowthresh"),
        (0.5, 1.0, 5, 60, 0.5, "tight_1gap_k5_1h"),
        (0.5, 1.0, 10, 60, 0.5, "tight_1gap_k10_1h"),
        (0.5, 1.0, 15, 60, 0.5, "tight_1gap_k15_1h"),
        (0.5, 1.0, 20, 60, 0.5, "tight_1gap_k20_1h"),
        (0.5, 1.5, 5, 60, 0.5, "tight_sl_1gap_k5_1h"),
        (0.5, 1.5, 10, 60, 0.5, "tight_sl_1gap_k10_1h"),
        (0.5, 1.5, 20, 60, 0.5, "tight_sl_1gap_k20_1h"),
        (1.0, 2.0, 3, 60, 0.5, "tight_1pct_gap_k3_1h"),
        (1.0, 2.0, 5, 60, 0.5, "tight_1pct_gap_k5_1h"),
        (1.0, 2.0, 10, 60, 0.5, "tight_1pct_gap_k10_1h"),
        (1.0, 2.0, 5, 30, 0.3, "tight_1pct_gap_k5_30m"),
        (1.0, 2.0, 10, 30, 0.3, "tight_1pct_gap_k10_30m"),
        (1.5, 3.0, 3, 120, 0.5, "med_1gap_k3_2h"),
        (1.5, 3.0, 5, 120, 0.5, "med_1gap_k5_2h"),
        (1.5, 3.0, 10, 120, 0.5, "med_1gap_k10_2h"),
        (1.5, 3.0, 3, 60, 0.3, "med_1gap_k3_1h_lowthresh"),
        (1.5, 3.0, 5, 60, 0.3, "med_1gap_k5_1h_lowthresh"),
        (1.5, 3.0, 10, 60, 0.3, "med_1gap_k10_1h_lowthresh"),
        (2.0, 4.0, 5, 120, 0.5, "wide_1gap_k5_2h"),
        (2.0, 4.0, 10, 120, 0.5, "wide_1gap_k10_2h"),
        (2.0, 4.0, 10, 120, 1.0, "wide_1pct_gap_k10_2h"),
    ]

    results_list = []
    for sl, tp, k, hold, gap, label in configs:
        try:
            r = backtest_1min(df_dict, sl_pct=sl, tp_pct=tp, k=k,
                              max_hold_bars=hold, min_gap=gap, min_vol_ratio=1.0)
            if "error" in r:
                P(f"  {label}: {r['error']}")
            else:
                marker = " ***" if r["sharpe"] > 2 and r["max_drawdown_pct"] < 10 else ""
                P(f"  {label}: ret={r['total_return_pct']:+.0f}% ann={r['annual_return_pct']:.0f}% "
                  f"Sharpe={r['sharpe']:.3f} DD={r['max_drawdown_pct']:.0f}% "
                  f"win={r['win_rate_pct']:.0f}% tr={r['n_trades']} rb={r['n_rebalances']} "
                  f"stop={r['stop_rate_pct']:.0f}% tgt={r['target_rate_pct']:.0f}% "
                  f"hold={r['hold_rate_pct']:.0f}% avg_gap={r['avg_gap_pct']:.1f}%{marker}")
                results_list.append({"label": label, "config": [sl, tp, k, hold, gap], **r})
        except Exception as e:
            P(f"  {label}: ERROR - {e}")
            traceback.print_exc()

    # Save
    out = ROOT / "data" / "research" / f"intraday_results_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.write_text(
        json.dumps({"symbols": list(df_dict.keys()),
                     "date_range": [str(from_dt), str(to_dt)],
                     "results": results_list}, indent=2, default=str),
        encoding="utf-8",
    )
    P(f"\nSaved results to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
