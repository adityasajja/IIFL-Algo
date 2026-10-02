"""Fetch 1-min data for additional liquid NIFTY stocks."""
import sys, time
from pathlib import Path
from datetime import datetime, timedelta

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

MORE_SYMS = ["AXISBANK", "SBIN", "BAJFINANCE", "BAJAJFINSV",
             "DRREDDY", "ASIANPAINT", "SUNPHARMA", "HCLTECH",
             "ITC", "JSWSTEEL", "ADANIPORTS", "CIPLA",
             "POWERGRID", "NTPC", "ONGC", "M&M"]

def main():
    s = get_settings()
    store = SessionStore(s.iifl_session_cache)
    session = store.load()
    if not session:
        P("NO SESSION"); return 1
    P(f"Session: {session.client_id}, expires {session.expires_at}")

    client = IiflClient(
        app_key=s.iifl_app_key, app_secret=s.iifl_app_secret,
        session_store=store, timeout=30,
    )
    client.set_session(session)

    P("Profile:", client.profile().get("result", {}).get("clientId", "???"))

    master = InstrumentMaster(client)
    P("Syncing instrument master...")
    master.sync(["NSEEQ"])

    cache = ROOT / "data" / "iifl_1min"
    from_dt = datetime(2025, 9, 1)
    to_dt = datetime(2026, 9, 15)

    fetched = []
    for sym in MORE_SYMS:
        fpath = cache / f"{sym}.parquet"
        if fpath.exists():
            df = pd.read_parquet(fpath)
            P(f"  {sym}: already cached ({len(df)} bars)")
            fetched.append(sym)
            continue

        inst = master.search(sym, exchange="NSEEQ")
        if inst.empty:
            P(f"  {sym}: NOT FOUND"); continue
        conid = str(inst.iloc[0]["conid"])

        rows = []
        cur = from_dt
        n_chunks = 0
        while cur < to_dt:
            end = min(cur + timedelta(days=45), to_dt)
            payload = client.historical_data(
                "NSEEQ", conid, "1m", _fmt_date(cur), _fmt_date(end),
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
            n_chunks += 1
            cur = end + timedelta(days=1)
            time.sleep(0.05)

        if not rows:
            P(f"  {sym}: no data"); continue

        df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
        df.to_parquet(fpath, index=False)
        fetched.append(sym)
        P(f"  {sym}: {len(df)} bars ({n_chunks} chunks)")
        time.sleep(1.0)  # be kind to the API

    P(f"\nFetched: {fetched}")
    P(f"Total cached stocks: {len(list(cache.glob('*.parquet')))}")
    client.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
