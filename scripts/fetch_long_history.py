"""Fetch long daily history for a symbol list into the local parquet cache.

The paper strategies need ~200-260 bars of warmup before the first scored bar,
so a fold-based walk-forward needs years, not the ~249 days the local cache
holds. This script writes ``data/iifl_daily/<EXCHANGE>/<SYMBOL>.parquet`` with
the same schema ``load_daily`` reads back, so nothing downstream changes.

Usage::

    .venv/Scripts/python.exe scripts/fetch_long_history.py \
        --symbols RELIANCE,INFY,... --exchange NSEEQ --days 2200
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from atr.brokers.iifl.client import IiflClient  # noqa: E402
from atr.brokers.iifl.contracts import InstrumentMaster  # noqa: E402
from atr.alerts.engine import IST  # noqa: E402

OHLCV = ["open", "high", "low", "close", "volume"]
CACHE_ROOT = Path(__file__).resolve().parents[1] / "data" / "iifl_daily"


def _candles_to_frame(payload) -> pd.DataFrame:
    """Reuse the canonical parser rather than re-deriving IIFL's envelope.

    IIFL wraps candles as ``{"status": "Ok", "result": [{"candles": [[iso, o, h, l, c, v], ...]}]}``.
    ``atr.signals.engine._candles_to_frame`` already handles that shape plus the
    list-of-dicts variants, so parsing lives in exactly one place.
    """
    from atr.signals.engine import _candles_to_frame as parse

    return parse(payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--exchange", default="NSEEQ")
    ap.add_argument("--days", type=int, default=2200)
    ap.add_argument("--pause", type=float, default=0.25)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    out_dir = CACHE_ROOT / args.exchange.upper()
    out_dir.mkdir(parents=True, exist_ok=True)

    client = IiflClient()
    if client.restore_session() is None:
        print("no cached IIFL session — run `atr login` first", file=sys.stderr)
        return 1
    master = InstrumentMaster(client)
    master.load_cached([args.exchange])

    to_date = datetime.now(IST)
    from_date = to_date - timedelta(days=args.days)
    ok = 0
    failed: list[str] = []

    # The NSEEQ master lists cash equities as "RELIANCE-EQ", while every other
    # layer of this codebase (cache filenames, scanner universe, watchlists)
    # uses the bare ticker. Try both spellings before giving up.
    suffixes = ("", "-EQ") if args.exchange.upper() == "NSEEQ" else ("",)

    with client:
        for i, symbol in enumerate(symbols, 1):
            conid = None
            for suffix in suffixes:
                with contextlib.suppress(KeyError):
                    conid = master.find(f"{symbol}{suffix}", args.exchange).conid
                    break
            if conid is None:
                failed.append(f"{symbol} (no conid)")
                continue
            try:
                payload = client.historical_data(
                    exchange=args.exchange,
                    instrument_id=str(conid),
                    interval="1d",
                    from_date=from_date,
                    to_date=to_date,
                )
                frame = _candles_to_frame(payload)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{symbol} ({exc})")
                continue

            if len(frame) < 300:
                failed.append(f"{symbol} (only {len(frame)} bars)")
                continue

            path = out_dir / f"{symbol}.parquet"
            frame.to_parquet(path, index=False)
            ok += 1
            print(f"[{i}/{len(symbols)}] {symbol}: {len(frame)} bars "
                  f"{frame['ts'].iloc[0].date()} -> {frame['ts'].iloc[-1].date()}")
            time.sleep(args.pause)

    print(f"\nwrote {ok}/{len(symbols)} symbols to {out_dir}")
    if failed:
        print("skipped:")
        for f in failed:
            print("  -", f)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
