"""Real-time market tick broadcaster and WebSocket stream manager.

Bridges decoded binary MQTT feeds from IIFL's live BridgeClient to
connected browser WebSocket clients, resolving contract IDs dynamically
and tracking the latest ticks with price change and direction.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import numpy as np
import polars as pl
from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from atr.config.settings import get_settings

@dataclass
class LiveTickPayload:
    symbol: str
    exchange: str
    ltp: float
    last_qty: int
    volume: int
    open: float
    high: float
    low: float
    close: float
    chg: float
    chg_pct: float
    best_bid: float | None
    best_ask: float | None
    ts: str
    depth: list[dict[str, Any]] | None = None

class TickBroadcaster:
    """Manages active WebSocket connections and multiplexes IIFL market ticks."""

    def __init__(self) -> None:
        self._active_connections: set[WebSocket] = set()
        self._client_subscriptions: dict[WebSocket, set[str]] = {}
        self._subscribed_symbols: dict[str, int] = {}  # symbol -> ref count
        self._symbol_to_topic: dict[str, str] = {}
        self._topic_to_symbol: dict[str, str] = {}
        self._symbol_to_exchange: dict[str, str] = {}
        self._latest_ticks: dict[str, LiveTickPayload] = {}
        self._prev_close: dict[str, float] = {}
        # In-memory time-series ring buffer: keeps last 5,000 raw ticks per symbol
        # O(1) appends, zero disk I/O, ultra-low sub-millisecond memory footprint
        self._ring_buffer: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=5000))

        self._bridge = None
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._master = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _get_master(self):
        if self._master is None:
            from atr.brokers.iifl.contracts import InstrumentMaster
            self._master = InstrumentMaster()
            try:
                self._master.load_cached(["NSEEQ", "BSEEQ"])
            except Exception as e:
                logger.warning("Could not load cached contracts in broadcaster: {}", e)
        return self._master

    def _ensure_bridge(self) -> bool:
        """Connect to the IIFL Bridge if a valid session exists."""
        with self._lock:
            if self._bridge is not None and getattr(self._bridge, "is_connected", False):
                return True

            try:
                settings = get_settings()
                from atr.brokers.iifl.auth import SessionStore
                from atr.brokers.iifl.bridge import BridgeClient

                session = SessionStore(settings.iifl_session_cache).load()
                if session is None or session.is_expired():
                    logger.debug("No active or unexpired IIFL session for BridgeClient")
                    return False

                logger.info("Initializing IIFL BridgeClient for live ticks...")
                bridge = BridgeClient(session)
                bridge.on_feed = self._on_bridge_feed
                bridge.on_error = lambda code, msg: logger.warning("Bridge tick error {}: {}", code, msg)
                bridge.connect(timeout=8.0)
                self._bridge = bridge

                # Resubscribe any existing symbols
                topics = list(self._symbol_to_topic.values())
                if topics:
                    self._bridge.subscribe_feed(topics)
                return True
            except Exception as exc:
                logger.warning("Could not connect IIFL BridgeClient: {}", exc)
                self._bridge = None
                return False

    def _resolve_symbol(self, symbol: str, exchange: str = "NSEEQ") -> str | None:
        """Resolve a trading symbol (e.g. RELIANCE-EQ) to topic like 'nseeq/2885'."""
        sym_upper = symbol.strip().upper()
        if sym_upper in self._symbol_to_topic:
            return self._symbol_to_topic[sym_upper]

        try:
            from atr.scanner import resolve_conid
            master = self._get_master()
            conid = resolve_conid(master, sym_upper, exchange.upper())
            prefix = "nseeq" if exchange.upper() == "NSEEQ" else exchange.lower()
            topic = f"{prefix}/{conid}"
            self._symbol_to_topic[sym_upper] = topic
            self._topic_to_symbol[topic] = sym_upper
            self._symbol_to_exchange[sym_upper] = exchange.upper()
            return topic
        except Exception as e:
            logger.debug("Failed to resolve conid for symbol {}: {}", symbol, e)
            return None

    def _on_bridge_feed(self, topic: str, feed) -> None:
        """Callback from BridgeClient thread when a 186-byte tick arrives."""
        symbol = self._topic_to_symbol.get(topic)
        if not symbol:
            return

        ltp = float(feed.ltp)
        prev = self._prev_close.get(symbol) or float(feed.close or ltp)
        if feed.close:
            self._prev_close[symbol] = float(feed.close)

        chg = round(ltp - prev, 2)
        chg_pct = round((chg / prev * 100) if prev > 0 else 0.0, 2)

        ts_val = feed.last_traded_time
        if isinstance(ts_val, datetime):
            ts_str = ts_val.isoformat()
        else:
            ts_str = datetime.now().isoformat()

        depth_list = []
        if getattr(feed, "depth", None):
            for d in feed.depth[:10]:
                depth_list.append({
                    "price": float(d.price),
                    "quantity": int(d.quantity),
                    "orders": int(d.orders),
                    "type": int(d.transaction_type),
                })

        payload = LiveTickPayload(
            symbol=symbol,
            exchange=self._symbol_to_exchange.get(symbol, "NSEEQ"),
            ltp=ltp,
            last_qty=int(feed.last_traded_quantity or 0),
            volume=int(feed.traded_volume or 0),
            open=float(feed.open or 0),
            high=float(feed.high or 0),
            low=float(feed.low or 0),
            close=float(feed.close or 0),
            chg=chg,
            chg_pct=chg_pct,
            best_bid=float(feed.best_bid_price) if feed.best_bid_price else None,
            best_ask=float(feed.best_ask_price) if feed.best_ask_price else None,
            ts=ts_str,
            depth=depth_list if depth_list else None,
        )

        with self._lock:
            self._latest_ticks[symbol] = payload
            self._ring_buffer[symbol].append({
                "epoch": time.time(),
                "ts": ts_str,
                "ltp": ltp,
                "qty": int(feed.last_traded_quantity or 0),
                "volume": int(feed.traded_volume or 0),
                "best_bid": float(feed.best_bid_price) if feed.best_bid_price else None,
                "best_ask": float(feed.best_ask_price) if feed.best_ask_price else None,
            })

        # Broadcast asynchronously to all interested WebSocket connections
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast_tick(payload), self._loop)

    async def _broadcast_tick(self, tick: LiveTickPayload) -> None:
        msg = json.dumps({"type": "tick", "data": asdict(tick)})
        dead_conns = []
        for ws, symbols in list(self._client_subscriptions.items()):
            if tick.symbol in symbols:
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead_conns.append(ws)

        for ws in dead_conns:
            self.disconnect(ws)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active_connections.add(ws)
        self._client_subscriptions[ws] = set()

        # Send initial status
        bridge_ok = self._ensure_bridge()
        await ws.send_text(json.dumps({
            "type": "status",
            "bridge_connected": bridge_ok,
            "server_time": datetime.now().isoformat()
        }))

    def disconnect(self, ws: WebSocket) -> None:
        self._active_connections.discard(ws)
        subs = self._client_subscriptions.pop(ws, set())
        to_unsub = []
        with self._lock:
            for s in subs:
                cnt = self._subscribed_symbols.get(s, 1) - 1
                if cnt <= 0:
                    self._subscribed_symbols.pop(s, None)
                    topic = self._symbol_to_topic.get(s)
                    if topic:
                        to_unsub.append(topic)
                else:
                    self._subscribed_symbols[s] = cnt

            if to_unsub and self._bridge and getattr(self._bridge, "is_connected", False):
                try:
                    self._bridge.unsubscribe_feed(to_unsub)
                except Exception:
                    pass

    async def subscribe(self, ws: WebSocket, symbols: list[str], exchange: str = "NSEEQ") -> None:
        if ws not in self._client_subscriptions:
            return

        to_subscribe_topics = []
        new_symbols_for_ws = []

        with self._lock:
            for s in symbols:
                sym_clean = s.strip().upper()
                if not sym_clean:
                    continue
                self._client_subscriptions[ws].add(sym_clean)
                new_symbols_for_ws.append(sym_clean)

                curr_cnt = self._subscribed_symbols.get(sym_clean, 0)
                if curr_cnt == 0:
                    topic = self._resolve_symbol(sym_clean, exchange)
                    if topic:
                        to_subscribe_topics.append(topic)
                self._subscribed_symbols[sym_clean] = curr_cnt + 1

        # Subscribe on bridge if new topics
        if to_subscribe_topics and self._ensure_bridge() and self._bridge:
            try:
                self._bridge.subscribe_feed(to_subscribe_topics)
            except Exception as e:
                logger.warning("Bridge subscribe error: {}", e)

        # Immediately return cached latest ticks if available
        for s in new_symbols_for_ws:
            cached = self._latest_ticks.get(s)
            if cached:
                try:
                    await ws.send_text(json.dumps({"type": "tick", "data": asdict(cached)}))
                except Exception:
                    break

    async def unsubscribe(self, ws: WebSocket, symbols: list[str]) -> None:
        if ws not in self._client_subscriptions:
            return

        to_unsub = []
        with self._lock:
            for s in symbols:
                sym_clean = s.strip().upper()
                if sym_clean in self._client_subscriptions[ws]:
                    self._client_subscriptions[ws].remove(sym_clean)
                    cnt = self._subscribed_symbols.get(sym_clean, 1) - 1
                    if cnt <= 0:
                        self._subscribed_symbols.pop(sym_clean, None)
                        topic = self._symbol_to_topic.get(sym_clean)
                        if topic:
                            to_unsub.append(topic)
                    else:
                        self._subscribed_symbols[sym_clean] = cnt

        if to_unsub and self._bridge and getattr(self._bridge, "is_connected", False):
            try:
                self._bridge.unsubscribe_feed(to_unsub)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # In-Memory Time-Series Ring Buffer Query Engine
    # ------------------------------------------------------------------
    def get_tick_history(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent ticks up to `limit` for a symbol."""
        sym_clean = symbol.strip().upper()
        with self._lock:
            buf = self._ring_buffer.get(sym_clean)
            if not buf:
                return []
            ticks = list(buf)
        return ticks[-limit:]

    def get_rolling_vwap(self, symbol: str, window_seconds: int = 900) -> dict[str, Any]:
        """Compute rolling Volume Weighted Average Price (VWAP) over window_seconds.

        Uses NumPy vectorization on the ring buffer snapshot for microsecond latency.
        """
        sym_clean = symbol.strip().upper()
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            buf = self._ring_buffer.get(sym_clean)
            if not buf:
                cached = self._latest_ticks.get(sym_clean)
                return {
                    "symbol": sym_clean,
                    "vwap": cached.ltp if cached else 0.0,
                    "ticks_count": 1 if cached else 0,
                    "total_volume": cached.volume if cached else 0,
                    "window_seconds": window_seconds,
                }
            items = [t for t in buf if t["epoch"] >= cutoff]

        if not items:
            latest = self._latest_ticks.get(sym_clean)
            return {
                "symbol": sym_clean,
                "vwap": latest.ltp if latest else 0.0,
                "ticks_count": 0,
                "total_volume": 0,
                "window_seconds": window_seconds,
            }

        prices = np.array([t["ltp"] for t in items], dtype=np.float64)
        quantities = np.array([max(t["qty"], 1) for t in items], dtype=np.float64)

        total_qty = np.sum(quantities)
        if total_qty > 0:
            vwap = float(np.sum(prices * quantities) / total_qty)
        else:
            vwap = float(np.mean(prices))

        return {
            "symbol": sym_clean,
            "vwap": round(vwap, 2),
            "ticks_count": len(items),
            "total_qty": int(total_qty),
            "window_seconds": window_seconds,
        }

    def get_tick_candles(self, symbol: str, interval_seconds: int = 5) -> list[dict[str, Any]]:
        """Resample in-memory ring buffer ticks into sub-minute OHLCV candles via Polars."""
        sym_clean = symbol.strip().upper()
        with self._lock:
            buf = self._ring_buffer.get(sym_clean)
            if not buf:
                return []
            items = list(buf)

        if not items:
            return []

        try:
            df = pl.DataFrame(items)
            # Group into interval_seconds time buckets
            df = df.with_columns(
                ((pl.col("epoch") // interval_seconds) * interval_seconds).alias("bucket")
            )
            agg = (
                df.group_by("bucket")
                .agg([
                    pl.col("ltp").first().alias("open"),
                    pl.col("ltp").max().alias("high"),
                    pl.col("ltp").min().alias("low"),
                    pl.col("ltp").last().alias("close"),
                    pl.col("qty").sum().alias("volume"),
                    pl.col("ts").last().alias("ts"),
                ])
                .sort("bucket")
            )
            return agg.to_dicts()
        except Exception as err:
            logger.warning("Error aggregating tick candles with Polars: {}", err)
            return []


_BROADCASTER: TickBroadcaster | None = None

def get_broadcaster() -> TickBroadcaster:
    global _BROADCASTER
    if _BROADCASTER is None:
        _BROADCASTER = TickBroadcaster()
    return _BROADCASTER
