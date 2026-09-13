"""Bulk historical download with a local parquet cache.

One parquet per symbol under ``data/iifl_daily/<EXCHANGE>/``. Re-runs are
cheap: files written today are skipped, so a nightly cron stays incremental.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from loguru import logger

from atr.brokers.iifl.client import IiflClient
from atr.brokers.iifl.contracts import InstrumentMaster
from atr.config.settings import get_settings

CACHE_ROOT = Path("data/iifl_daily")
COLUMNS = ["ts", "open", "high", "low", "close", "volume"]

_local = threading.local()


def _client() -> IiflClient:
    if getattr(_local, "client", None) is None:
        s = get_settings()
        c = IiflClient(app_key=s.iifl_app_key, app_secret=s.iifl_app_secret,
                       base_url=s.iifl_base_url)
        if c.restore_session() is None:
            raise RuntimeError("no active IIFL session — run `atr login`")
        _local.client = c
    return _local.client


def universe(exchange: str = "NSEEQ", series: str = "EQ") -> pd.DataFrame:
    """All `<SYMBOL>-<SERIES>` contracts, e.g. 2654 NSEEQ equities."""
    master = InstrumentMaster()
    master.load_cached([exchange.upper()])
    df = master.frame
    df = df[df["exchange"].str.upper() == exchange.upper()]
    if series:
        df = df[df["symbol"].str.endswith(f"-{series.upper()}")]
    return df[["symbol", "conid"]].drop_duplicates().sort_values("symbol").reset_index(drop=True)


def _sync_one(exchange: str, symbol: str, conid: str, interval: str,
              from_date: str, to_date: str, outdir: Path, pause: float) -> str:
    import time as _time

    path = outdir / f"{symbol}.parquet"
    if path.exists():
        try:
            if date.fromtimestamp(path.stat().st_mtime) >= date.today():
                return "skip"
        except OSError:
            pass
    try:
        raw = _client().historical_data(exchange, str(conid), interval, from_date, to_date)
        candles = raw["result"][0]["candles"]
        df = pd.DataFrame(candles, columns=COLUMNS)
        if df.empty:
            return "empty"
        df["ts"] = pd.to_datetime(df["ts"])
        df.sort_values("ts").to_parquet(path, index=False)
        status = "ok"
    except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the job
        logger.warning("{}: {}", symbol, str(exc)[:120])
        status = "fail"
    _time.sleep(pause)
    return status


def sync_all(exchange: str = "NSEEQ", interval: str = "1d",
             from_date: str | None = None, to_date: str | None = None,
             workers: int = 6, pause: float = 0.2,
             start: int = 0, end: int | None = None) -> Counter:
    """Download a slice [start:end] of the universe. Returns status counts."""
    to_date = to_date or date.today().strftime("%d-%b-%Y")
    from_date = from_date or (date.today() - timedelta(days=365)).strftime("%d-%b-%Y")
    uni = universe(exchange).iloc[start:end]
    outdir = CACHE_ROOT / exchange.upper()
    outdir.mkdir(parents=True, exist_ok=True)
    logger.info("syncing {} {} {} bars ({} symbols, {} workers)",
                exchange, interval, f"{from_date}->{to_date}", len(uni), workers)
    counts: Counter = Counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(_sync_one, exchange.upper(), r.symbol, r.conid,
                        interval, from_date, to_date, outdir, pause): r.symbol
            for r in uni.itertuples()
        }
        for i, fut in enumerate(as_completed(futs), 1):
            counts[fut.result()] += 1
            if i % 250 == 0:
                logger.info("{}/{} … {}", i, len(futs), dict(counts))
    logger.info("done: {}", dict(counts))
    return counts


def load_cached(
    exchange: str = "NSEEQ", symbols: Iterable[str] | None = None
) -> dict[str, pd.DataFrame]:
    """All cached frames for an exchange: {symbol: df}.

    Pass ``symbols`` to read only those files. Uses Polars when available
    for multi-threaded Rust Arrow deserialization (~2.5x faster).
    """
    outdir = CACHE_ROOT / exchange.upper()
    if symbols is None:
        paths = sorted(outdir.glob("*.parquet"))
    else:
        paths = [outdir / f"{s.upper()}.parquet" for s in symbols]

    try:
        import polars as pl
        use_polars = True
    except ImportError:
        use_polars = False

    frames = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            if use_polars:
                frames[path.stem] = pl.read_parquet(path).to_pandas()
            else:
                frames[path.stem] = pd.read_parquet(path)
        except Exception:  # noqa: BLE001 — skip corrupt files
            continue
    return frames
