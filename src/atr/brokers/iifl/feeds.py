"""Historical and live market data feeds backed by IIFL Capital."""

from __future__ import annotations

import queue
import threading
from datetime import datetime

import pandas as pd
from loguru import logger

from atr.brokers.iifl.auth import Session
from atr.brokers.iifl.bridge import BridgeClient
from atr.brokers.iifl.client import IiflClient
from atr.core.enums import Timeframe
from atr.core.models import Bar, Instrument, MarketSnapshot, Tick
from atr.data.aggregator import TickToBarAggregator
from atr.data.base import DataFeed, pivot_to_snapshots

_TOPIC = {
    "NSEEQ": "nseeq",
    "BSEEQ": "bseeq",
    "NSEFO": "nsefo",
    "BSEFO": "bsefo",
    "NSECOMM": "nsecomm",
    "MCXCOMM": "mcxcomm",
    "NSECURR": "nsecurr",
    "BSECURR": "bsecurr",
    "INDICES": "nseeq",
}


def exchange_topic(exchange: str) -> str:
    return _TOPIC.get(exchange.upper(), exchange.lower())


def bridge_topic(instrument: Instrument) -> str:
    if instrument.conid is None:
        raise ValueError(f"instrument {instrument.symbol} has no instrumentId")
    return f"{exchange_topic(instrument.exchange)}/{instrument.conid}"


class IiflHistoricalFeed(DataFeed):
    """Loads candles via POST /marketdata/historicaldata for backtests.

    ``instruments`` is a mapping of symbol -> Instrument (each Instrument must
    carry ``conid``, i.e. the IIFL instrumentId).
    """

    def __init__(
        self,
        client: IiflClient,
        instruments: dict[str, Instrument],
        interval: str = "1m",
        from_date: datetime | str = "01-Jan-2024",
        to_date: datetime | str = "31-Dec-2024",
        timeframe: Timeframe = Timeframe.MIN_1,
    ) -> None:
        self.client = client
        self.instruments_map = instruments
        self.interval = interval
        self.from_date = from_date
        self.to_date = to_date
        self.timeframe = timeframe
        self._snapshots: list[MarketSnapshot] | None = None

    @property
    def instruments(self) -> dict[str, Instrument]:
        return self.instruments_map

    def fetch(self) -> pd.DataFrame:
        frames = []
        for symbol, inst in self.instruments_map.items():
            logger.info("fetching {} {} candles for {}", len([symbol]), self.interval, symbol)
            rows = self.client.historical_data(
                exchange=inst.exchange,
                instrument_id=str(inst.conid),
                interval=self.interval,
                from_date=self.from_date,
                to_date=self.to_date,
            )
            for row in rows:
                frames.append(
                    {
                        "ts": pd.to_datetime(row.get("initialTimestamp")),
                        "symbol": symbol,
                        "open": float(row.get("open", 0)),
                        "high": float(row.get("high", 0)),
                        "low": float(row.get("low", 0)),
                        "close": float(row.get("close", 0)),
                        "volume": float(row.get("volume", 0) or 0),
                    }
                )
        return pd.DataFrame(frames).sort_values(["ts", "symbol"]).reset_index(drop=True)

    def load(self) -> list[MarketSnapshot]:
        if self._snapshots is None:
            self._snapshots = pivot_to_snapshots(self.fetch(), self.timeframe)
        return self._snapshots

    def save_parquet(self, path: str) -> None:
        self.fetch().to_parquet(path, index=False)


class IiflLiveFeed:
    """Realtime ticks over the MQTT bridge, aggregated into bars.

    Usage::

        feed = IiflLiveFeed(session, instruments, freq="1min")
        feed.start()
        snapshot = feed.get(timeout=1.0)   # MarketSnapshot
        feed.stop()
    """

    def __init__(
        self,
        session: Session,
        instruments: dict[str, Instrument],
        freq: str = "1min",
        maxsize: int = 10_000,
    ) -> None:
        self.instruments = instruments
        self._topics = {bridge_topic(i): sym for sym, i in instruments.items()}
        self._queues: dict[str, queue.Queue] = {}
        self._bridge: BridgeClient | None = None
        self._session = session
        self._snapshots: queue.Queue = queue.Queue(maxsize=maxsize)
        self._aggregators: dict[str, TickToBarAggregator] = {}
        self._bars_by_symbol: dict[str, Bar | None] = {}
        self._last_tick: dict[str, Tick] = {}
        self._lock = threading.Lock()
        self.freq = freq

    # ------------------------------------------------------------------
    def start(self, subscribe_oi: bool = False) -> None:
        self._bridge = BridgeClient(self._session)
        self._bridge.on_feed = self._on_feed
        self._bridge.on_error = lambda code, msg: logger.error("bridge error {}: {}", code, msg)
        self._bridge.connect()

        for symbol in self.instruments:
            self._aggregators[symbol] = TickToBarAggregator(self.freq, on_bar=None)

        self._bridge.subscribe_feed(list(self._topics))
        if subscribe_oi:
            fno = [t for t, s in self._topics.items() if self.instruments[s].is_derivative]
            if fno:
                self._bridge.subscribe_open_interest(fno)
        self._bridge.subscribe_order_updates()
        self._bridge.subscribe_trade_updates()

    def stop(self) -> None:
        if self._bridge:
            self._bridge.disconnect()

    # ------------------------------------------------------------------
    def _on_feed(self, topic: str, feed) -> None:
        symbol = self._topics.get(topic)
        if symbol is None:
            return
        ts = feed.last_traded_time
        tick = Tick(
            ts=ts,
            last=feed.ltp,
            bid=feed.best_bid_price or None,
            ask=feed.best_ask_price or None,
            volume=feed.last_traded_quantity,
        )
        aggregator = self._aggregators.get(symbol)
        if aggregator is None:
            return
        completed = aggregator.update(ts, feed.ltp, feed.last_traded_quantity)
        with self._lock:
            self._last_tick[symbol] = tick
            self._bars_by_symbol[symbol] = aggregator.current_bar()
            if completed is not None:
                self._snapshots.put(
                    MarketSnapshot(ts=completed.ts, bars={symbol: completed})
                )

    def get(self, timeout: float = 1.0) -> MarketSnapshot | None:
        try:
            return self._snapshots.get(timeout=timeout)
        except queue.Empty:
            return None

    def last_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._last_tick.get(symbol)

    def latest(self) -> MarketSnapshot | None:
        with self._lock:
            bars = {s: b for s, b in self._bars_by_symbol.items() if b is not None}
        if not bars:
            return None
        return MarketSnapshot(ts=max(b.ts for b in bars.values()), bars=bars)
