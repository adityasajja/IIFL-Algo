"""Real-time market tick broadcaster and WebSocket stream manager.

Bridges decoded binary MQTT feeds from IIFL's live BridgeClient to
connected browser WebSocket clients, resolving contract IDs dynamically
and tracking the latest ticks with price change and direction.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

try:
    import orjson

    def _fast_dumps(obj: Any) -> str:
        return orjson.dumps(obj).decode("utf-8")
except ImportError:
    def _fast_dumps(obj: Any) -> str:
        return json.dumps(obj)

import numpy as np
import polars as pl
from fastapi import WebSocket
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
    epoch: float = 0.0  # Unix timestamp for precise candle alignment
    depth: list[dict[str, Any]] | None = None

def _normalise_universe(
    symbols: Iterable[str] | Mapping[str, Iterable[str]], exchange: str
) -> dict[str, set[str]]:
    """Coerce a symbol universe to ``{exchange: {SYMBOL, ...}}``.

    Accepts either a flat iterable — every symbol on one ``exchange`` — or a
    mapping of exchange to symbols, which is what a caller with deployments on
    more than one exchange has to say. Both are normalised here rather than at
    the call sites so a subscription can never be sent with a symbol whose
    exchange was guessed.
    """
    if isinstance(symbols, Mapping):
        out: dict[str, set[str]] = {}
        for key, values in symbols.items():
            cleaned = {
                str(s).strip().upper() for s in (values or ()) if str(s).strip()
            }
            if cleaned:
                out[str(key).strip().upper()] = cleaned
        return out

    cleaned = {str(s).strip().upper() for s in (symbols or ()) if str(s).strip()}
    return {exchange.strip().upper(): cleaned} if cleaned else {}


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
        self._tick_recv_time: dict[str, float] = {}  # symbol -> time.time() of last tick
        self._prev_close: dict[str, float] = {}
        #: The universe a *server-side* consumer wants on the feed — today that
        #: is the paper runner, whose deployments must be priced whether or not
        #: anybody has a dashboard open. A set rather than a ref count because
        #: the consumer states the universe it wants, in full, every pass; the
        #: previous statement is the only state needed to compute the delta.
        self._runner_symbols: set[str] = set()
        #: The subset of that universe actually confirmed onto the bridge. Kept
        #: apart from the wish above so a statement made while the bridge was
        #: down is *retried* on the next one, instead of being remembered as
        #: done — a deployment that is silently not on the feed is the failure
        #: this whole path exists to remove.
        self._runner_acked: set[str] = set()
        #: Symbols that resolved to no contract. Remembered so an unresolvable
        #: symbol is attempted once per appearance rather than re-resolved on
        #: every pass, and forgotten as soon as it leaves the universe so a
        #: corrected symbol list gets a fresh attempt.
        self._runner_unresolved: set[str] = set()
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

                # Resubscribe all known topics
                all_topics = set()
                for sym, cnt in self._subscribed_symbols.items():
                    if cnt > 0:
                        t = self._symbol_to_topic.get(sym) or self._resolve_symbol(sym)
                        if t:
                            all_topics.add(t)
                for sym in self._runner_symbols:
                    t = self._symbol_to_topic.get(sym) or self._resolve_symbol(sym)
                    if t:
                        all_topics.add(t)
                for t in self._symbol_to_topic.values():
                    all_topics.add(t)

                if all_topics:
                    self._bridge.subscribe_feed(list(all_topics))
                    logger.info("BridgeClient subscribed {} active topics on init", len(all_topics))
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

        epoch_now = time.time()
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
            epoch=epoch_now,
            depth=depth_list if depth_list else None,
        )

        with self._lock:
            self._latest_ticks[symbol] = payload
            self._tick_recv_time[symbol] = epoch_now
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
        msg = _fast_dumps({"type": "tick", "data": asdict(tick)})
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
        await ws.send_text(_fast_dumps({
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
                    # Only take the symbol off the feed if nobody else wants it.
                    # A paper deployment's universe is a reason to stay
                    # subscribed that has nothing to do with this socket.
                    if s not in self._runner_symbols:
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

        # Immediately return cached latest tick if it is fresh (< 60 s old).
        # A tick from a previous session must never be sent as live data.
        _now = time.time()
        for s in new_symbols_for_ws:
            cached = self._latest_ticks.get(s)
            recv_t = self._tick_recv_time.get(s, 0.0)
            if cached and (_now - recv_t) < 60.0:
                try:
                    await ws.send_text(_fast_dumps({"type": "tick", "data": asdict(cached)}))
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
                        # The paper runner's universe keeps the symbol on the
                        # feed after the last browser lets it go.
                        if sym_clean not in self._runner_symbols:
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
    # Server-side subscriptions — a consumer that is not a browser
    # ------------------------------------------------------------------
    def ensure_symbols(
        self,
        symbols: Iterable[str] | Mapping[str, Iterable[str]],
        exchange: str = "NSEEQ",
    ) -> dict[str, list[str]]:
        """Put a universe on the feed on behalf of a server-side consumer.

        **This is the difference between a paper deployment that trades and one
        that only looks like it does.** A symbol reached the feed only when a
        *browser* asked for it: :meth:`_resolve_symbol` is called from
        :meth:`subscribe` and nowhere else, so with no dashboard open the bridge
        was never connected, no tick ever arrived, and :meth:`latest_price`
        returned ``None`` for every symbol. The paper venue reads "no price" as a
        rejection for a new order and as "resting" for an existing one, so the
        observable result was a deployment that reported itself as running while
        evaluating nothing — and, because ``default_price_source`` falls back to
        the daily cache, one that would have filled at yesterday's close if it
        filled at all. That is a paper fill at a price no exchange offered, which
        is the one thing a paper account must never do.

        **Declarative, not incremental.** The caller states the universe it
        wants, in full; the difference against the previous statement is what is
        sent to the bridge. The runner re-states its universe on every pass, so
        the steady state is a set comparison and no I/O.

        **Separate from the browser ref counts**, so the two kinds of consumer
        cannot unsubscribe each other. Closing a dashboard tab must not stop a
        paper deployment from being priced, and stopping a deployment must not
        blank a chart somebody is watching.

        **A failed statement is retried, not remembered as done.** The desired
        universe and the universe confirmed onto the bridge are tracked
        separately, so a call made while the bridge was down leaves its symbols
        outstanding and the next call sends them again.

        Returns ``{"added", "removed", "unresolved"}`` — the symbols newly put on
        the feed, those taken off, and those that could not be resolved to a
        contract. The last is reported rather than swallowed: a symbol with no
        conid is a deployment that will never be priced from live ticks, and an
        empty list is how a caller tells that apart from success.
        """
        wanted_by_exchange = _normalise_universe(symbols, exchange)
        wanted = {s for group in wanted_by_exchange.values() for s in group}

        with self._lock:
            fresh = wanted - self._runner_acked - self._runner_unresolved
            dropped = self._runner_acked - wanted
            self._runner_symbols = wanted
            # A symbol that has left the universe is forgotten entirely, so
            # putting it back is a new attempt rather than a remembered failure.
            self._runner_unresolved &= wanted
            held = {s: self._subscribed_symbols.get(s, 0) for s in wanted | dropped}

        added: list[str] = []
        unresolved: list[str] = []
        topics: list[str] = []

        # Resolution happens *outside* the lock. ``_resolve_symbol`` may load the
        # instrument master, and holding the lock across that would stall tick
        # ingestion from the bridge thread — the very ticks this method exists to
        # obtain.
        for group_exchange, group in sorted(wanted_by_exchange.items()):
            for symbol in sorted(group):
                if symbol not in fresh:
                    continue
                topic = self._symbol_to_topic.get(symbol) or self._resolve_symbol(
                    symbol, group_exchange
                )
                if topic is None:
                    unresolved.append(symbol)
                    continue
                added.append(symbol)
                # Only reach the exchange if no browser is already carrying it.
                if held.get(symbol, 0) == 0:
                    topics.append(topic)

        removed: list[str] = []
        drop_topics: list[str] = []
        for symbol in sorted(dropped):
            removed.append(symbol)
            if held.get(symbol, 0) == 0:
                topic = self._symbol_to_topic.get(symbol)
                if topic:
                    drop_topics.append(topic)

        if topics or drop_topics:
            subscribed = False
            if self._ensure_bridge() and self._bridge:
                try:
                    if topics:
                        self._bridge.subscribe_feed(topics)
                    if drop_topics:
                        self._bridge.unsubscribe_feed(drop_topics)
                    subscribed = True
                except Exception as e:  # noqa: BLE001 - the next pass retries
                    logger.warning(
                        "Bridge subscription for the paper universe failed: {}", e
                    )

            if subscribed:
                with self._lock:
                    self._runner_acked |= {s for s in added}
                    self._runner_acked -= set(removed)
            else:
                # Nothing reached the bridge, so nothing is acked: the symbols
                # stay outstanding and the next statement of this universe sends
                # them again rather than assuming the job was done.
                with self._lock:
                    self._runner_acked -= set(removed)

        if unresolved:
            # Attempted once per appearance. Re-resolving a symbol that has no
            # contract on every pass would cost a lookup a second and print the
            # same warning forever; the caller reports it once, which is the
            # signal an operator can act on.
            with self._lock:
                self._runner_unresolved |= set(unresolved)

        return {"added": added, "removed": removed, "unresolved": unresolved}

    # ------------------------------------------------------------------
    # In-Memory Time-Series Ring Buffer Query Engine
    # ------------------------------------------------------------------
    def latest_price(self, symbol: str) -> float | None:
        """The most recent traded price for a symbol, or ``None``.

        The seam the paper engine reads so a fill is priced off the market
        rather than off yesterday's close. Returns the whole number or nothing:
        a NaN or a zero here would become the cost basis of a position, and from
        there every P&L figure downstream.

        Read under the lock because the bridge writes ``_latest_ticks`` from its
        own MQTT thread — a dict mutation concurrent with this read is exactly
        the race the lock exists for.
        """
        sym = symbol.strip().upper()
        with self._lock:
            tick = self._latest_ticks.get(sym)
            recv_t = self._tick_recv_time.get(sym, 0.0)
        if tick is None:
            return None
        # Reject stale ticks — do not let a price from a previous session
        # become the cost basis of a paper fill.
        if (time.time() - recv_t) > 60.0:
            return None
        price = float(tick.ltp)
        return price if price == price and price > 0 else None

    def latest_tick(self, symbol: str) -> LiveTickPayload | None:
        """The most recent full tick payload for a symbol, or None."""
        sym = symbol.strip().upper()
        with self._lock:
            return self._latest_ticks.get(sym)

    def latest_price_and_time(self, symbol: str) -> tuple[float | None, datetime | None]:
        """Return (ltp, tick_datetime) for a symbol."""
        sym = symbol.strip().upper()
        with self._lock:
            tick = self._latest_ticks.get(sym)
        if tick is None:
            return None, None
        price = float(tick.ltp)
        if not (price == price and price > 0):
            return None, None
        dt = None
        if tick.ts:
            try:
                dt = datetime.fromisoformat(tick.ts)
            except (ValueError, TypeError):
                dt = None
        return price, dt

    def latest_prices(self) -> dict[str, float]:
        """Every symbol with a live price, as ``{symbol: ltp}``.

        One snapshot for the whole book rather than N lookups, so marking a
        deployment's positions does not re-acquire the lock per position.
        """
        with self._lock:
            items = list(self._latest_ticks.items())
        out: dict[str, float] = {}
        for symbol, tick in items:
            price = float(tick.ltp)
            if price == price and price > 0:
                out[symbol] = price
        return out

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
        """Resample in-memory ring buffer ticks into sub-minute OHLCV candles via DuckDB."""
        sym_clean = symbol.strip().upper()
        with self._lock:
            buf = self._ring_buffer.get(sym_clean)
            if not buf:
                return []
            items = list(buf)

        if not items:
            return []

        try:
            from atr.analytics.duck_engine import get_duck_engine
            duck = get_duck_engine()
            res = duck.resample_candles(sym_clean, ticks=items, interval_seconds=interval_seconds)
            if res:
                return res
        except Exception as err:
            logger.debug("DuckDB candle aggregation fallback: {}", err)

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
            logger.warning("Error aggregating tick candles: {}", err)
            return []


_BROADCASTER: TickBroadcaster | None = None

def get_broadcaster() -> TickBroadcaster:
    global _BROADCASTER
    if _BROADCASTER is None:
        _BROADCASTER = TickBroadcaster()
    return _BROADCASTER
