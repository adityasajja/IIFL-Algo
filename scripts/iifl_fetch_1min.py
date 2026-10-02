"""Fetch 1-minute OHLCV from IIFL and run the intraday gap-and-run backtest.

Uses the cached session (valid until midnight IST). Fetches 1-minute candles
for liquid NIFTY 50 + NIFTY NEXT 50 stocks, then backtests the gap-and-run
signal at 1-minute frequency with intraday-appropriate SL/TP/exit timing.
"""
from __future__ import annotations
import json
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atr.brokers.iifl.auth import SessionStore, Session  # noqa: E402
from atr.brokers.iifl.client import IiflClient  # noqa: E402
from atr.brokers.iifl.contracts import InstrumentMaster  # noqa: E402
from atr.config.settings import get_settings  # noqa: E402

p = print
def P(*a, **kw): kw.setdefault("flush", True); p(*a, **kw)

# ── 1-minute fetcher ─────────────────────────────────────────────────────────

def fetch_1min(client, exchange, instrument_id, symbol, from_dt, to_dt,
               max_days=25, sleep=0.15):
    """Fetch 1-minute candles in chunks (API limit ~25 trading days per request)."""
    from atr.brokers.iifl.client import _fmt_date
    from_dt_str = _fmt_date(from_dt)
    to_dt_str = _fmt_date(to_dt)

    # Parse the from/to to figure out chunks
    chunks = []
    cur = from_dt
    while cur < to_dt:
        end = min(cur + timedelta(days=max_days), to_dt)
        chunks.append((cur, end))
        cur = end + timedelta(days=1)

    all_candles = []
    for i, (c_from, c_to) in enumerate(chunks):
        try:
            payload = client.historical_data(
                exchange=exchange,
                instrument_id=str(instrument_id),
                interval="1m",
                from_date=_fmt_date(c_from),
                to_date=_fmt_date(c_to),
            )
            # Extract candles from response
            candles = []
            if isinstance(payload, dict):
                result = payload.get("result", payload)
                if isinstance(result, dict):
                    candles = result.get("candles", [])
                elif isinstance(result, list):
                    if result and isinstance(result[0], dict):
                        candles = result[0].get("candles", []) if "candles" in result[0] else result
                    elif result and isinstance(result[0], list):
                        candles = result
            elif isinstance(payload, list):
                candles = payload

            for row in candles:
                if isinstance(row, (list, tuple)) and len(row) >= 6:
                    all_candles.append({
                        "ts": pd.to_datetime(row[0]),
                        "symbol": symbol,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                    })
                elif isinstance(row, dict):
                    ts = row.get("initialTimestamp") or row.get("timestamp") or row.get("ts")
                    if ts:
                        all_candles.append({
                            "ts": pd.to_datetime(ts),
                            "symbol": symbol,
                            "open": float(row.get("open", 0) or 0),
                            "high": float(row.get("high", 0) or 0),
                            "low": float(row.get("low", 0) or 0),
                            "close": float(row.get("close", 0) or 0),
                            "volume": float(row.get("volume", 0) or 0),
                        })

            P(f"  {symbol} chunk {i+1}/{len(chunks)}: {len(candles)} candles")
            time.sleep(sleep)
        except Exception as e:
            P(f"  {symbol} chunk {i+1} FAILED: {e}")
            traceback.print_exc()
            time.sleep(sleep)

    P(f"  {symbol}: total {len(all_candles)} 1-min candles")
    return all_candles


def load_universe():
    """Load stock symbols from universe files."""
    syms = set()
    for f in [ROOT / "data" / "universe" / "nifty50.txt",
              ROOT / "data" / "universe" / "mid150.txt"]:
        if f.exists():
            for line in f.read_text().strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    syms.add(line)
    return sorted(syms)


def fetch_universe_1min(client, symbols, from_dt, to_dt, exchange="NSEEQ",
                        cache_dir="data/iifl_1min"):
    """Fetch 1-minute data for all symbols, caching to parquet."""
    cache = ROOT / cache_dir
    cache.mkdir(parents=True, exist_ok=True)

    # Load instrument master to get conids
    master = InstrumentMaster(client)
    P("Syncing instrument master...")
    master.sync([exchange])

    results = {}
    for sym in symbols:
        try:
            instruments = master.search(sym, exchange=exchange)
            if instruments.empty:
                P(f"  {sym}: NOT FOUND in instrument master")
                continue
            conid = str(instruments.iloc[0]["conid"])
            candles = fetch_1min(client, exchange, conid, sym, from_dt, to_dt)
            if not candles:
                P(f"  {sym}: no data returned")
                continue
            df = pd.DataFrame(candles).sort_values("ts").reset_index(drop=True)
            df.to_parquet(cache / f"{sym}.parquet", index=False)
            results[sym] = len(df)
            P(f"  {sym}: saved {len(df)} candles to {cache / f'{sym}.parquet'}")
        except Exception as e:
            P(f"  {sym}: FAILED - {e}")
            traceback.print_exc()

    return results


# ── Intraday backtest engine ─────────────────────────────────────────────────

def backtest_1min(df_dict, nifty_df=None, sl_pct=1.5, tp_pct=3.0,
                  k=5, max_hold_bars=120, costs=0.001, min_gap=0.5,
                  min_mom=0.0, min_vol_ratio=1.0):
    """Backtest gap-and-run on 1-minute bars.

    Args:
        df_dict: {symbol: DataFrame with [ts, open, high, low, close, volume]}
        nifty_df: NIFTY 1-min data for regime filter
        sl_pct: stop-loss percentage (intraday)
        tp_pct: take-profit percentage (intraday)
        k: number of positions
        max_hold_bars: max bars to hold (120 = 2 hours)
    """
    from datetime import dtime as dtime
    # Resample to daily for signal detection
    daily_dict = {}
    for sym, df in df_dict.items():
        d = df.copy()
        d["date"] = d["ts"].dt.date
        agg = d.groupby("date").agg(
            open=("open", "first"), high=("high", "max"),
            low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
        ).reset_index()
        agg["date"] = pd.to_datetime(agg["date"])
        daily_dict[sym] = agg

    # Get all trading dates
    all_dates = sorted(set(d["date"].iloc[0] for d in daily_dict.values()))

    # Build daily close panel for momentum
    close_daily = pd.DataFrame({
        sym: d.set_index("date")["close"].reindex(all_dates) for sym, d in daily_dict.items()
    }).ffill()

    # 5-day momentum
    mom5 = close_daily / close_daily.shift(5) - 1.0

    # NIFTY regime filter
    if nifty_df is not None:
        n_daily = nifty_df.copy()
        n_daily["date"] = n_daily["ts"].dt.date
        n_agg = n_daily.groupby("date").agg(close=("close", "last")).reset_index()
        n_agg["date"] = pd.to_datetime(n_agg["date"])
        n_agg = n_agg.set_index("date").reindex(all_dates).ffill()
        n_sma200 = n_agg["close"].rolling(200).mean()
        regime_ok = n_agg["close"] > n_sma200

    # Gap: open vs prev close
    open_daily = pd.DataFrame({
        sym: d.set_index("date")["open"].reindex(all_dates) for sym, d in daily_dict.values()
    }).ffill()
    gap = (open_daily / close_daily.shift(1) - 1.0)

    # Volume ratio
    vol_daily = pd.DataFrame({
        sym: d.set_index("date")["volume"].reindex(all_dates) for sym, d in daily_dict.values()
    }).ffill()
    vol_avg = vol_daily.rolling(20).mean()
    vol_ratio = vol_daily / vol_avg

    # Find signal dates
    scores = (gap - 0.005) * mom5 * vol_ratio
    # Signal: gap >= min_gap, mom >= min_mom, vol_ratio >= min_vol_ratio
    signal_mask = (gap >= min_gap / 100.0) & (mom5 >= min_mom / 100.0) & (vol_ratio >= min_vol_ratio)
    if nifty_df is not None:
        signal_mask = signal_mask & regime_ok.values.reshape(-1, 1)

    # Backtest: at each signal date, enter at open and simulate exits
    weekly_returns = []
    all_trades = []
    entry_idx = 0

    signal_dates = all_dates[20:]  # warmup
    for i, date in enumerate(signal_dates):
        if date not in close_daily.index:
            continue
        idx = close_daily.index.get_loc(date)
        if idx < 20:
            continue

        if nifty_df is not None and not regime_ok.iloc[idx]:
            continue

        day_scores = scores.iloc[idx]
        day_mask = signal_mask.iloc[idx]
        valid = day_mask & close_daily.iloc[idx].notna()
        if valid.sum() < k:
            continue

        picks = day_scores[valid].nlargest(k).index.tolist()
        for sym in picks:
            entry_price = open_daily.iloc[idx][sym]
            if np.isnan(entry_price) or entry_price <= 0:
                continue

            # Simulate intraday exit: find 1-min path after open
            intraday = df_dict[sym]
            day_data = intraday[intraday["ts"].dt.date == all_dates[idx].date()]
            if len(day_data) < 2:
                continue

            entry_bar = day_data.iloc[0]
            holding = day_data.iloc[1:]

            exit_price = None
            exit_reason = "max_hold"
            for j, row in holding.iterrows():
                bars_held = j
                # SL
                if row["low"] <= entry_price * (1 - sl_pct / 100):
                    exit_price = entry_price * (1 - sl_pct / 100)
                    exit_reason = "stop"
                    break
                # TP
                if row["high"] >= entry_price * (1 + tp_pct / 100):
                    exit_price = entry_price * (1 + tp_pct / 100)
                    exit_reason = "target"
                    break
                # Max hold
                if bars_held >= max_hold_bars:
                    exit_price = row["close"]
                    exit_reason = "max_hold"
                    break
            else:
                exit_price = holding.iloc[-1]["close"] if len(holding) > 0 else entry_price

            ret = (exit_price - entry_price) / entry_price - costs
            all_trades.append({
                "date": str(date.date()), "symbol": sym,
                "entry": entry_price, "exit": exit_price,
                "ret": ret, "reason": exit_reason,
                "gap": gap.iloc[idx][sym],
            })

    if not all_trades:
        return {"error": "no trades"}

    df_trades = pd.DataFrame(all_trades)
    n_rb = len(df_trades) // k
    weekly_ret = df_trades.groupby("date")["ret"].mean().values  # per-rebalance portfolio ret

    total = 1.0
    returns = []
    for r in weekly_ret:
        total *= (1 + r)
        returns.append(r)

    returns = np.array(returns)
    if len(returns) < 3:
        return {"error": "insufficient rebalances"}

    # Convert to weekly equivalent (assumes 5 rebalances/week at intraday freq)
    # With daily entry, each entry is a "week" if we rebalance every 5 trading days
    cum = np.cumprod(1 + returns) - 1
    total_ret = float(cum[-1])
    n = len(returns)
    mean = float(np.mean(returns))
    std = float(np.std(returns, ddof=1)) if n > 1 else 0
    sharpe = (mean / std * np.sqrt(52 / n * n)) if std > 0 else 0  # simplified
    # Better: treat each rebalance as ~0.2 week
    ppy = 252 / n  # rebalancing frequency in years
    ann_ret = mean * 252  # if daily rebalancing
    ann_vol = std * np.sqrt(252) if std > 0 else 0
    sharpe = (ann_ret - 0.075) / ann_vol if ann_vol > 0 else 0
    rmax = np.maximum.accumulate(cum + 1)
    max_dd = float(np.min((cum + 1) / rmax - 1))

    win = float(np.mean(returns > 0))
    avg_trade = float(np.mean(df_trades["ret"]))

    return {
        "n_trades": len(df_trades),
        "n_rebalances": n,
        "avg_trade_pct": round(100 * avg_trade, 3),
        "win_rate_pct": round(100 * float((df_trades["ret"] > 0).mean()), 1),
        "total_return_pct": round(100 * total_ret, 2),
        "annual_return_pct": round(100 * ann_ret, 1),
        "annual_vol_pct": round(100 * ann_vol, 1),
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(100 * max_dd, 2),
        "positive_pct": round(100 * win, 1),
        "stop_rate_pct": round(100 * float((df_trades["reason"] == "stop").mean()), 1),
        "target_rate_pct": round(100 * float((df_trades["reason"] == "target").mean()), 1),
        "hold_rate_pct": round(100 * float((df_trades["reason"] == "max_hold").mean()), 1),
        "avg_gap_pct": round(100 * float(df_trades["gap"].mean()), 2),
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    settings = get_settings()
    store = SessionStore(settings.iifl_session_cache)
    session = store.load()
    if not session:
        P("No cached session! Open the login URL first:")
        P(f"  {login_url(settings.iifl_app_key, settings.iifl_redirect_url)}")
        return 1

    P(f"Session valid: client_id={session.client_id}, expires={session.expires_at}")

    client = IiflClient(
        app_key=settings.iifl_app_key,
        app_secret=settings.iifl_app_secret,
        base_url=settings.iifl_base_url,
        session_store=store,
    )
    client.set_session(session)

    # Verify auth works
    try:
        profile = client.profile()
        P(f"Authenticated as: {profile}")
    except Exception as e:
        P(f"Auth test failed: {e}")
        # Try restoring
        sess = client.restore_session()
        if sess:
            P(f"Restored session: {sess.client_id}")
        else:
            P("Cannot restore session. Exiting.")
            return 1

    # Load universe
    universe = load_universe()
    P(f"Universe: {len(universe)} symbols")

    # Fetch NIFTYBEES for regime filter
    master = InstrumentMaster(client)
    master.sync(["NSEEQ"])

    # Test with 1 stock first to verify the API works
    from_dt = datetime(2025, 9, 1)
    to_dt = datetime(2026, 9, 15)

    test_syms = ["RELIANCE", "HDFCBANK", "INFY", "ICICIBANK", "TCS", "NIFTYBEES", "HINDUNILVR"]
    P(f"Fetching 1-minute data for {len(test_syms)} symbols ({from_dt.date()} → {to_dt.date()})...")

    results = fetch_universe_1min(client, test_syms, from_dt, to_dt, exchange="NSEEQ")
    P(f"\nFetch results: {results}")

    # Load the cached data
    cache = ROOT / "data" / "iifl_1min"
    df_dict = {}
    for sym, count in results.items():
        f = cache / f"{sym}.parquet"
        if f.exists():
            df_dict[sym] = pd.read_parquet(f)

    if "NIFTYBEES" in df_dict:
        nifty_df = df_dict.pop("NIFTYBEES")
    else:
        nifty_df = None

    P(f"\nLoaded {len(df_dict)} stocks with 1-minute data")
    for sym, df in df_dict.items():
        P(f"  {sym}: {len(df)} bars, {df['ts'].min()} → {df['ts'].max()}")

    if not df_dict:
        P("No data fetched. Exiting.")
        return 1

    # Run intraday backtest
    P(f"\n{'='*70}")
    P(f"INTRADAY GAP-AND-RUN BACKTEST (1-minute data)")
    P(f"{'='*70}")

    # Test parameter combinations
    for sl, tp, k_pos, max_hold, gap_thresh in [
        (1.0, 2.0, 3, 60, 0.5),   # tight SL/TP, fast exit (1h)
        (1.5, 3.0, 5, 120, 0.5),  # moderate (2h)
        (2.0, 4.0, 5, 180, 1.0),  # wider, 3h
        (1.0, 2.0, 3, 60, 1.0),   # 1% gap threshold
        (1.5, 3.0, 5, 120, 1.0),  # 1% gap, moderate
        (2.0, 4.0, 5, 120, 2.0),  # 2% gap only (run_5 equivalent)
    ]:
        r = backtest_1min(
            df_dict, nifty_df=nifty_df,
            sl_pct=sl, tp_pct=tp, k=k_pos, max_hold_bars=max_hold,
            min_gap=gap_thresh, min_mom=0.0, min_vol_ratio=1.0,
        )
        if "error" in r:
            P(f"  sl={sl}% tp={tp}% k={k_pos} hold={max_hold}min_gap={gap_thresh}%: {r['error']}")
        else:
            marker = "***" if 1 <= r.get("total_return_pct", 0) <= 10 else "   "
            P(f"  {marker} sl={sl}% tp={tp}% k={k_pos} hold={max_hold}min_gap={gap_thresh}%: "
              f"ret={r['total_return_pct']:.0f}% ann_ret={r['annual_return_pct']:.0f}% "
              f"Sharpe={r['sharpe']:.3f} DD={r['max_drawdown_pct']:.0f}% "
              f"trades={r['n_trades']} rb={r['n_rebalances']} "
              f"stop={r['stop_rate_pct']:.0f}% tgt={r['target_rate_pct']:.0f}% "
              f"hold={r['hold_rate_pct']:.0f}% win={r['win_rate_pct']:.0f}%")

    client.close()
    P("\nDone!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
