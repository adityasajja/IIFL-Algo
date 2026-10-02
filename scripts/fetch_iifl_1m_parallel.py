"""Parallel 1-minute fetcher for IIFL — fetches NIFTY 50 stocks concurrently."""
import json, time, sys, traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
from atr.brokers.iifl.client import IiflClient, _fmt_date
from atr.brokers.iifl.auth import Session, SessionStore
from atr.brokers.iifl.feeds import _candles, _candle_row

UNIVERSE_FILES = [ROOT / "data" / "universe" / "n50.txt",
                  ROOT / "data" / "universe" / "mid150.txt"]
CONTRACTS_FILE = ROOT / ".cache" / "contracts" / "NSEEQ.json"
OUT_DIR = ROOT / "data" / "iifl_1m"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FROM_DATE = "01-Jan-2024"
TO_DATE = "19-Sep-2026"
INTERVAL = "1m"
N_WORKERS = 4


def load_universe() -> list[str]:
    syms = []
    for f in UNIVERSE_FILES:
        if f.exists():
            content = f.read_text().strip()
            syms.extend(s.strip() for s in content.replace("\n", ",").split(",") if s.strip())
    return list(dict.fromkeys(syms))


def load_contracts() -> dict[str, str]:
    rows = json.loads(CONTRACTS_FILE.read_text())
    m = {}
    for r in rows:
        sym = r.get("underlyingInstrumentSymbol") or r.get("tradingSymbol", "")
        iid = r.get("instrumentId")
        if sym and iid:
            key = sym.replace("-EQ", "").replace("-BE", "").replace("-SM", "")
            m[key] = iid
    return m


def fetch_one(symbol, iid, client):
    out_path = OUT_DIR / f"{symbol}.parquet"
    if out_path.exists():
        df = pd.read_parquet(out_path)
        if len(df) > 5000:
            return symbol, f"skip ({len(df)} bars)"

    payload = client.historical_data(
        exchange="NSEEQ", instrument_id=iid, interval=INTERVAL,
        from_date=FROM_DATE, to_date=TO_DATE,
    )
    result = payload.get("result", []) if isinstance(payload, dict) else []
    if isinstance(result, list) and result and isinstance(result[0], dict):
        candles = result[0].get("candles", [])
    elif isinstance(result, dict):
        candles = result.get("candles", [])
    else:
        candles = result

    rows = []
    for c in candles:
        if isinstance(c, (list, tuple)) and len(c) >= 6:
            rows.append({
                "ts": pd.to_datetime(c[0]),
                "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                "close": float(c[4]), "volume": float(c[5]),
            })

    if not rows:
        return symbol, "NO DATA"
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    df.to_parquet(out_path, index=False)
    return symbol, f"{len(df)} bars ({df['ts'].iloc[0].date()}→{df['ts'].iloc[-1].date()})"


client = IiflClient()
store = SessionStore(str(ROOT / ".cache" / "iifl_session.json"))
sess = store.load()
if sess is None:
    print("ERROR: No cached IIFL session")
    sys.exit(1)
client.set_session(sess)
print(f"Session OK (client_id={sess.client_id})")

universe = load_universe()
contracts = load_contracts()
pairs = [(s, contracts[s]) for s in universe if s in contracts]
print(f"Universe: {len(universe)} symbols, {len(pairs)} have instrument IDs")

# Fetch in parallel
fetched = 0
errors = 0
with ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
    futures = {pool.submit(fetch_one, s, iid, client): s for s, iid in pairs}
    done = 0
    for fut in as_completed(futures, timeout=3600):
        done += 1
        sym = fut._result_name if hasattr(fut, '_result_name') else futures[fut]
        try:
            symbol, msg = fut.result()
            print(f"  [{done}/{len(pairs)}] {symbol}: {msg}", flush=True)
            if "bars" in msg:
                fetched += 1
            elif "skip" in msg:
                fetched += 1
            else:
                errors += 1
        except Exception as e:
            print(f"  [{done}/{len(pairs)}] {futures[fut]}: ERROR — {e}", flush=True)
            errors += 1

print(f"\nDone: {fetched} fetched, {errors} errors")
total_files = len(list(OUT_DIR.glob("*.parquet")))
total_bars = sum(len(pd.read_parquet(f)) for f in OUT_DIR.glob("*.parquet"))
print(f"Total cached: {total_files} files, ~{total_bars:,} bars")
