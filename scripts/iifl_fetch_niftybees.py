"""Fetch 1-min NIFTYBEES data from IIFL API using cached session."""
import sys, os
sys.path.insert(0, "src")
os.chdir("D:\\ALGO")

import pandas as pd
from datetime import datetime
from atr.brokers.iifl.auth import SessionStore
from atr.brokers.iifl.client import IiflClient, _fmt_date
from atr.brokers.iifl.contracts import InstrumentMaster
from atr.config.settings import get_settings

s = get_settings()
store = SessionStore(s.iifl_session_cache)
session = store.load()
if not session:
    print("NO_SESSION")
    sys.exit(1)

print(f"Session: {session.client_id} expires {session.expires_at}", flush=True)

client = IiflClient(
    app_key=s.iifl_app_key,
    app_secret=s.iifl_app_secret,
    session_store=store,
    timeout=30,
)
client.set_session(session)

print("Calling profile...", flush=True)
p = client.profile()
print(f"Profile OK: clientId={p['result']['clientId'] if 'result' in p else p}", flush=True)

master = InstrumentMaster(client)
print("Syncing instruments...", flush=True)
master.sync(["NSEEQ"])

inst = master.search("NIFTYBEES", exchange="NSEEQ")
conid = str(inst.iloc[0]["conid"])
print(f"NIFTYBEES conid={conid}", flush=True)

from_dt = datetime(2026, 8, 15)
to_dt = datetime(2026, 9, 15)
print(f"Fetching 1min {from_dt.date()} -> {to_dt.date()}...", flush=True)

payload = client.historical_data(
    exchange="NSEEQ",
    instrument_id=conid,
    interval="1m",
    from_date=_fmt_date(from_dt),
    to_date=_fmt_date(to_dt),
)
print(f"Payload type: {type(payload)}", flush=True)

candles = []
if isinstance(payload, dict):
    result = payload.get("result", payload)
    if isinstance(result, dict):
        candles = result.get("candles", [])
    elif isinstance(result, list):
        candles = result

print(f"Got {len(candles)} candles", flush=True)
if candles:
    print(f"First: {candles[0]}", flush=True)
    print(f"Last: {candles[-1]}", flush=True)
    rows = []
    for r in candles:
        if isinstance(r, (list, tuple)) and len(r) >= 6:
            rows.append({"ts": pd.to_datetime(r[0]), "open": float(r[1]),
                         "high": float(r[2]), "low": float(r[3]),
                         "close": float(r[4]), "volume": float(r[5])})
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    df.to_parquet("data/iifl_1min/NIFTYBEES.parquet", index=False)
    print(f"Saved {len(df)} bars", flush=True)

client.close()
print("DONE", flush=True)
