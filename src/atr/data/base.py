"""Feed abstractions.

A feed converts some source of truth into an ordered sequence of
:class:`MarketSnapshot`. The backtester only ever consumes snapshots, which
keeps it completely decoupled from where data came from — CSV, TimescaleDB,
or IBKR.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import numpy as np
import pandas as pd

from atr.core.enums import Timeframe
from atr.core.models import Bar, Instrument, MarketSnapshot


class DataFeed(ABC):
    """Produces market data for backtests."""

    @abstractmethod
    def load(self) -> list[MarketSnapshot]:
        """Return snapshots sorted ascending by timestamp."""

    @property
    @abstractmethod
    def instruments(self) -> dict[str, Instrument]:
        ...

    def to_frame(self) -> pd.DataFrame:
        """Convenience: flatten snapshots into a tidy DataFrame."""
        rows = []
        for snap in self.load():
            for symbol, bar in snap.bars.items():
                rows.append(
                    {
                        "ts": snap.ts,
                        "symbol": symbol,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                    }
                )
        return pd.DataFrame(rows).sort_values(["ts", "symbol"]).reset_index(drop=True)


class ListFeed(DataFeed):
    """In-memory feed over pre-built snapshots.

    Lets one loaded history be carved into walk-forward train/test folds
    without reloading or regenerating the underlying data.
    """

    def __init__(
        self,
        snapshots: list[MarketSnapshot],
        instruments: dict[str, Instrument],
    ) -> None:
        self._snapshots = snapshots
        self._instruments = instruments

    @property
    def instruments(self) -> dict[str, Instrument]:
        return self._instruments

    def load(self) -> list[MarketSnapshot]:
        return self._snapshots


class LiveFeed(ABC):
    """Push-based feed for realtime operation."""

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def subscribe(self, instruments: list[Instrument]) -> None: ...

    @abstractmethod
    async def stream(self):  # pragma: no cover - interface only
        """Async iterator yielding MarketSnapshot objects."""
        yield  # type: ignore[misc]

    @abstractmethod
    async def close(self) -> None: ...


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def pivot_to_snapshots(df: pd.DataFrame, timeframe: Timeframe) -> list[MarketSnapshot]:
    """Convert a long DataFrame (ts, symbol, ohlcv) into ordered snapshots.

    This is the hot path for every backtest — a full-market run pushes millions
    of bars through it — so it stays on numpy arrays and makes a single pass.
    Grouping is done by walking the sorted timestamps rather than by
    ``groupby("ts")``; the latter builds one group object per bar, which
    dominated total runtime (measured ~40s of a 69s backtest).
    """
    required = {"ts", "symbol", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {sorted(missing)}")

    if df.empty:
        return []

    # Stable sort keeps the original symbol ordering within a timestamp.
    work = df.sort_values("ts", kind="mergesort")
    n = len(work)

    symbols = work["symbol"].to_numpy()
    opens = work["open"].to_numpy(dtype=float)
    highs = work["high"].to_numpy(dtype=float)
    lows = work["low"].to_numpy(dtype=float)
    closes = work["close"].to_numpy(dtype=float)
    volumes = (
        np.nan_to_num(work["volume"].to_numpy(dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
        if "volume" in work.columns
        else np.zeros(n)
    )
    ts_values = work["ts"].to_numpy()

    # One group per distinct timestamp. ``np.unique`` sorts, and the frame
    # already is sorted, so ``starts`` are each timestamp's first index.
    unique_ts, starts = np.unique(ts_values, return_index=True)
    timestamps = pd.DatetimeIndex(unique_ts).to_pydatetime()
    stops = np.append(starts[1:], n)

    snapshots: list[MarketSnapshot] = []
    for group, start, stop in zip(range(len(starts)), starts, stops, strict=True):
        ts = timestamps[group]
        bars: dict[str, Bar] = {}
        for j in range(start, stop):
            bars[symbols[j]] = Bar(
                ts=ts,
                open=opens[j],
                high=highs[j],
                low=lows[j],
                close=closes[j],
                volume=volumes[j],
            )
        snapshots.append(MarketSnapshot(ts=ts, bars=bars))
    return snapshots


def filter_snapshots(
    snapshots: list[MarketSnapshot], start: datetime | None, end: datetime | None
) -> list[MarketSnapshot]:
    out = snapshots
    if start is not None:
        out = [s for s in out if s.ts >= start]
    if end is not None:
        out = [s for s in out if s.ts <= end]
    return out
