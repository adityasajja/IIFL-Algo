"""Tick → bar aggregation for live trading and for building custom timeframes
from a finer base series."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from atr.core.models import Bar


@dataclass
class BarBuilder:
    """Accumulates ticks into a single bar."""

    ts: datetime
    open: float = 0.0
    high: float = float("-inf")
    low: float = float("inf")
    close: float = 0.0
    volume: float = 0.0
    _started: bool = False

    def update(self, price: float, size: float = 0.0) -> None:
        if not self._started:
            self.open = price
            self._started = True
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += size

    def to_bar(self) -> Bar:
        return Bar(
            ts=self.ts,
            open=self.open,
            high=self.high if self._started else self.close,
            low=self.low if self._started else self.close,
            close=self.close,
            volume=self.volume,
        )


class TickToBarAggregator:
    """Buckets a stream of (timestamp, price, size) into fixed-width bars.

    Emits a bar when a tick arrives in a later bucket, or on :meth:`flush`.
    """

    def __init__(self, freq: str = "1min", on_bar=None) -> None:
        self.freq = freq
        self.on_bar = on_bar
        self._current: BarBuilder | None = None
        self._bucket: datetime | None = None

    def update(self, ts: datetime, price: float, size: float = 0.0) -> Bar | None:
        bucket = pd.Timestamp(ts).floor(self.freq).to_pydatetime()
        completed = None
        if self._bucket is not None and bucket > self._bucket:
            completed = self._current.to_bar() if self._current else None
            self._current = None
        if self._current is None:
            self._bucket = bucket
            self._current = BarBuilder(ts=bucket)
        self._current.update(price, size)
        if completed is not None and self.on_bar:
            self.on_bar(completed)
        return completed

    def current_bar(self) -> Bar | None:
        """The in-progress bar (for dashboards / latest-price lookups)."""
        return self._current.to_bar() if self._current else None

    def flush(self) -> Bar | None:
        if self._current is None:
            return None
        bar = self._current.to_bar()
        self._current = None
        self._bucket = None
        return bar


@dataclass
class RollingWindow:
    """Fixed-size ring buffer of recent bars, handy inside strategies."""

    size: int = 500
    _items: list = field(default_factory=list, init=False)

    def push(self, bar: Bar) -> None:
        self._items.append(bar)
        if len(self._items) > self.size:
            del self._items[0]

    def closes(self) -> list[float]:
        return [b.close for b in self._items]

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "ts": b.ts,
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in self._items
            ]
        )

    def __len__(self) -> int:
        return len(self._items)
