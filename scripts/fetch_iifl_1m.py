"""Fetch 1-minute candles from IIFL for the NIFTY 50 + Midcap 150 universe.

Uses the cached JWT session (no browser re-auth needed if still valid).
Writes parquet files to data/iifl_1m/<SYMBOL>.parquet.
"""
import json, time, sys, traceback
from pathlib import Path
from datetime import datetime

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atr.brokers.iifl.client import IiflClient
from atr.brokers.iifl.auth import Session, SessionStore

# ------------------------------------------------------------------ config
UNIVERSE_FILES = [ROOT / "data" / "universe" / "n50.txt",
                  ROOT / "data" / "universe" / "mid150.txt"]
CONTRACTS_FILE = ROOT / ".cache" / "contracts" / "NSEEQ.json"
OUT_DIR = ROOT / "data" / "iifl_1m"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FROM_DATE = "01-Jan-2024"
TO_DATE = "19-Sep-2026"   # full history
INTERVAL = "1m"

# ------------------------------------------------------------------ helpers
def load_universe() -> list[str]:
    syms: list[str] = []
    for f in UNIVERSE_FILES:
        if f.exists():
            content = f.read_text().strip()
            # Files are comma-separated (may span multiple lines)
            parts = [s.strip() for s in content.replace("\n", ",").split(",") if s.strip()]
            syms.extend(parts)
    return list(dict.fromkeys(syms))  # de-dup, preserve order


def load_contracts() -> dict[str, str]:
    """Return {symbol: instrument_id} for NSEEQ equities."""
    rows = json.loads(CONTRACTS_FILE.read_text())
    m: dict[str, str] = {}
    for r in rows:
        sym = r.get("underlyingInstrumentSymbol") or r.get("tradingSymbol", "")
        iid = r.get("instrumentId")
        if sym and iid:
            m[sym.replace("-EQ", "").replace("-BE", "").replace("-SM", "")] = iid
    return m


def main() -> int:
    universe = load_universe()
    contracts = load_contracts()
    total = len(universe)

    # Build symbol → instrument_id
    pairs = [(s, contracts[s]) for s in universe if s in contracts]
    missing = [s for s in universe if s not in contracts]
    print(f"Universe: {total} symbols, {len(pairs)} have instrument IDs, "
          f"{len(missing)} missing contracts")
    if missing:
        print(f"  Missing: {missing[:20]}")

    # Restore session
    store = SessionStore(str(ROOT / ".cache" / "iifl_session.json"))
    sess = store.load()
    if sess is None:
        print("ERROR: No cached IIFL session found. Browser OAuth required.")
        return 1

    client = IiflClient()
    client.restore_session()
    print(f"Session restored for client_id={sess.client_id}")
    print(f"Starting 1-minute fetch for {len(pairs)} symbols "
          f"({FROM_DATE} → {TO_DATE})")

    fetched = 0
    errors = 0
    for i, (symbol, iid) in enumerate(pairs, 1):
        out_path = OUT_DIR / f"{symbol}.parquet"
        # Skip if we already have a reasonably full file
        if out_path.exists():
            try:
                df = pd.read_parquet(out_path)
                if len(df) > 5000:
                    print(f"  [{i}/{total}] {symbol}: skip ({len(df)} bars cached)")
                    continue
            except Exception:
                pass

        try:
            payload = client.historical_data(
                exchange="NSEEQ",
                instrument_id=iid,
                interval=INTERVAL,
                from_date=FROM_DATE,
                to_date=TO_DATE,
            )
            # Parse candles
            rows = []
            result = payload.get("result", []) if isinstance(payload, dict) else []
            if isinstance(result, list) and result and isinstance(result[0], dict):
                candles = result[0].get("candles", [])
            elif isinstance(result, dict):
                candles = result.get("candles", [])
            else:
                candles = result

            for c in candles:
                if isinstance(c, (list, tuple)) and len(c) >= 6:
                    rows.append({
                        "ts": pd.to_datetime(c[0]),
                        "open": float(c[1]),
                        "high": float(c[2]),
                        "low": float(c[3]),
                        "close": float(c[4]),
                        "volume": float(c[5]),
                    })

            if not rows:
                print(f"  [{i}/{total}] {symbol}: NO DATA (NOT FOUND)")
                errors += 1
                continue

            df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
            df.to_parquet(out_path, index=False)
            fetched += 1
            print(f"  [{i}/{total}] {symbol}: {len(df)} bars "
                  f"({df['ts'].iloc[0].date()} → {df['ts'].iloc[-1].date()})")

            # Rate limiting — IIFL API is sensitive
            time.sleep(0.5)

        except Exception as e:
            errors += 1
            print(f"  [{i}/{total}] {symbol}: ERROR — {e}")
            traceback.print_exc(limit=3)
            time.sleep(0.5)  # pause on error

    print(f"\n{'='*60}")
    print(f"Fetch complete: {fetched} symbols fetched, {errors} errors")
    print(f"Data saved to {OUT_DIR}")

    # Count total bars
    total_bars = 0
    n_files = 0
    for f in sorted(OUT_DIR.glob("*.parquet")):
        df = pd.read_parquet(f)
        total_bars += len(df)
        n_files += 1
    print(f"Total: {n_files} files, ~{total_bars:,} bars")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
