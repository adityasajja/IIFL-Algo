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
from functools import lru_cache
from pathlib import Path

import pandas as pd
import polars as pl
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


@lru_cache(maxsize=4)
def _cached_master_frame(exchange: str) -> pd.DataFrame:
    master = InstrumentMaster()
    return master.load_cached([exchange.upper()])


def universe(exchange: str = "NSEEQ", series: str = "EQ") -> pl.DataFrame:
    """All `<SYMBOL>-<SERIES>` contracts, e.g. 2654 NSEEQ equities."""
    frame = _cached_master_frame(exchange.upper())
    df = pl.from_pandas(frame)
    df = df.filter(pl.col("exchange").str.to_uppercase() == exchange.upper())
    if series:
        df = df.filter(pl.col("symbol").str.ends_with(f"-{series.upper()}"))
    return df.select(["symbol", "conid"]).unique().sort("symbol")


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
        df = pl.DataFrame(candles, schema=COLUMNS, orient="row")
        if df.is_empty():
            return "empty"
        if df.schema["ts"] == pl.Utf8:
            df = df.with_columns(pl.col("ts").str.to_datetime(strict=False))
        else:
            df = df.with_columns(pl.col("ts").cast(pl.Datetime))
        df.sort("ts").write_parquet(path)
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
    uni = universe(exchange)[start:end]
    outdir = CACHE_ROOT / exchange.upper()
    outdir.mkdir(parents=True, exist_ok=True)
    logger.info("syncing {} {} {} bars ({} symbols, {} workers)",
                exchange, interval, f"{from_date}->{to_date}", len(uni), workers)
    counts: Counter = Counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(_sync_one, exchange.upper(), r["symbol"], r["conid"],
                        interval, from_date, to_date, outdir, pause): r["symbol"]
            for r in uni.iter_rows(named=True)
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

    Pass ``symbols`` to read only those files. Reads via Polars (multi-threaded
    Rust Arrow deserialization, ~2.5x faster than pandas' own parquet reader),
    then hands back pandas — every consumer of this cache is still pandas-based.
    """
    from atr.data import panel

    outdir = CACHE_ROOT / exchange.upper()
    # One columnar file, current with the per-symbol ones (see atr.data.panel):
    # ~0.1s instead of ~5s for the whole exchange. Copies, because callers here
    # have always been free to modify what they are handed.
    shared = panel.frames(outdir)
    if symbols is None:
        return {s: df.copy() for s, df in shared.items()}
    return {s.upper(): shared[s.upper()].copy() for s in symbols if s.upper() in shared}
