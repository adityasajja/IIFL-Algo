"""Daily candles from the local price cache, for charts that should not need a broker login.

The chart used to fail with "no active IIFL session" even though every daily bar it wanted was
already on disk. Daily, weekly and monthly bars are served from the cache when the broker cannot
be reached. Intraday bars are not: the cache has none, and inventing them would be worse than
an error.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

#: Intervals the daily cache can answer. Weekly and monthly are resampled from the daily bars.
DAILY_INTERVALS = frozenset({"1d", "1w", "1mo"})

_RESAMPLE = {"1w": "W-FRI", "1mo": "ME"}


def _parse(value: str | None, default: date) -> date:
    if not value:
        return default
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return default


def cached_candles(
    symbol: str,
    exchange: str,
    interval: str,
    from_date: str | None,
    to_date: str | None,
    *,
    cache_root: Path | None = None,
) -> dict[str, Any] | None:
    """Candles from the local cache in the chart's wire shape, or None if it cannot answer.

    None means "ask the broker instead", never "there were no bars in the window".
    """
    if interval not in DAILY_INTERVALS:
        return None
    from atr.instruments.service import get_instrument_master

    try:
        master = get_instrument_master()
        record = master.get(symbol.upper(), exchange.upper())
        cache_file = getattr(record, "cache_file", None)
        if not cache_file:
            return None
        # The master knows where its own cache lives; a hard-coded root would read the wrong
        # directory whenever the master was built over another one.
        path = Path(cache_root if cache_root is not None else master.cache_root) / cache_file
        if not path.exists():
            return None
        frame = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - an unknown symbol or unreadable file is "cannot answer"
        logger.debug("chart cache: {} unavailable: {}", symbol, exc)
        return None
    if frame.empty or not {"ts", "open", "high", "low", "close"} <= set(frame.columns):
        return None

    frame = frame.assign(ts=pd.to_datetime(frame["ts"])).sort_values("ts").drop_duplicates("ts")
    last_day = frame["ts"].iloc[-1].date()
    start = _parse(from_date, date(1900, 1, 1))
    end = _parse(to_date, last_day)
    frame = frame[(frame["ts"].dt.date >= start) & (frame["ts"].dt.date <= end)]
    if frame.empty:
        return None
    if interval in _RESAMPLE:
        volume = frame["volume"] if "volume" in frame.columns else pd.Series(0.0, index=frame.index)
        frame = (
            frame.assign(volume=volume).set_index("ts")
            .resample(_RESAMPLE[interval])
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            .dropna(subset=["close"])
            .reset_index()
        )
    elif "volume" not in frame.columns:
        frame = frame.assign(volume=0.0)

    candles = [
        {
            "ts": t.strftime("%Y-%m-%dT%H:%M:%S"),
            "open": float(o), "high": float(h), "low": float(low), "close": float(c), "volume": float(v),
        }
        for t, o, h, low, c, v in zip(frame["ts"], frame["open"], frame["high"], frame["low"], frame["close"], frame["volume"], strict=True)
    ]
    return {
        "symbol": symbol.upper(),
        "exchange": exchange.upper(),
        "interval": interval,
        "candles": candles,
        "source": "local_cache",
        "as_of": last_day.isoformat(),
    }
