"""Candles, symbols, quotes and live ticks."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect


from atr.api.legacy.common import _authed_client, _broker_error, _broker_rows, _clean, _with_ip_hint

logger = logging.getLogger("atr.api")

router = APIRouter()


@router.get("/candles")
def candles(
    symbol: str = "RELIANCE-EQ",
    exchange: str = "NSEEQ",
    interval: str = "1d",
    from_date: str = "01-Mar-2026",
    to_date: str | None = None,
) -> dict[str, Any]:
    """Raw OHLCV candles for charting. Interval accepts 1m/5m/15m/30m/60m/1d."""
    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.scanner import resolve_conid

    client = _authed_client()
    master = InstrumentMaster(client)
    master.load_cached([exchange.upper()])
    conid = resolve_conid(master, symbol.upper(), exchange.upper())
    raw = client.historical_data(exchange.upper(), conid, interval, from_date,
                                 to_date or date.today().strftime("%d-%b-%Y"))
    out = [
        {"ts": c[0], "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
        for c in raw["result"][0]["candles"]
    ]
    return {"symbol": symbol.upper(), "exchange": exchange.upper(),
            "interval": interval, "candles": out}


_SYMBOL_MASTERS: dict[str, tuple[float, Any]] = {}
_SYMBOL_MASTER_TTL_S = 300.0


def _symbol_master(exchange: str) -> Any:
    """The instrument master for symbol search, loaded once per few minutes.

    Search fires on every keystroke; rebuilding a ~10k-row master from disk each
    time made the box lag. A five-minute TTL still picks up a fresh sync.
    """
    from atr.brokers.iifl.contracts import InstrumentMaster

    now = time.monotonic()
    hit = _SYMBOL_MASTERS.get(exchange)
    if hit and now - hit[0] < _SYMBOL_MASTER_TTL_S:
        return hit[1]
    master = InstrumentMaster()
    try:
        master.load_cached([exchange])
    except Exception as exc:  # noqa: BLE001 — cold cache, no client to refresh with
        raise HTTPException(503, f"instrument cache is cold — run `atr instruments sync` ({exc})")
    _SYMBOL_MASTERS[exchange] = (now, master)
    return master


@router.get("/symbols")
def symbols(query: str = "", exchange: str = "NSEEQ", limit: int = Query(25, ge=1, le=100)) -> dict[str, Any]:
    """Symbol search for the chart header. Served from the cached instrument
    master — no broker session needed."""
    master = _symbol_master(exchange.upper())
    hits = master.search(query.upper() or "EQ", exchange=exchange.upper(), limit=limit)
    return {
        "results": [
            {"symbol": r.symbol, "exchange": r.exchange, "conid": str(r.conid)}
            for r in hits.itertuples()
        ]
    }


@router.get("/quote")
def quote(symbols: str, exchange: str = "NSEEQ") -> dict[str, Any]:
    """Live quotes for a comma-separated symbol list, e.g. ``?symbols=RELIANCE-EQ,INFY-EQ``."""
    from atr.brokers.iifl.contracts import InstrumentMaster
    from atr.scanner import resolve_conid

    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not wanted:
        raise HTTPException(400, "pass ?symbols=RELIANCE-EQ,INFY-EQ")
    if len(wanted) > 50:
        raise HTTPException(400, "at most 50 symbols per request")

    client = _authed_client()
    master = InstrumentMaster(client)
    try:
        master.load_cached([exchange.upper()])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            503,
            f"no cached instrument master for {exchange} — run "
            f"`atr instruments sync --exchanges {exchange}` ({exc})",
        ) from exc

    legs, resolved, failed = [], [], []
    for symbol in wanted:
        try:
            legs.append((exchange.upper(), resolve_conid(master, symbol, exchange.upper())))
            resolved.append(symbol)
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the call
            failed.append({"symbol": symbol, "error": str(exc)[:160]})
    if not legs:
        raise HTTPException(404, f"could not resolve any of {wanted} on {exchange}")

    try:
        payload = client.market_quotes(legs)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"quote request failed: {exc}") from exc

    broker_error = _broker_error(payload)
    if broker_error:
        raise HTTPException(502, _with_ip_hint(broker_error))

    rows = _broker_rows(payload)
    for symbol, row in zip(resolved, rows, strict=False):
        row.setdefault("symbol", symbol)
    return {
        "exchange": exchange.upper(),
        "as_of": datetime.now().isoformat(),
        "quotes": _clean(rows),
        "failed": failed,
    }


def watchlist_live_quotes(
    symbols: list[str], exchange: str = "NSEEQ"
) -> dict[str, dict[str, Any]]:
    """Batch quote overlay for the watchlist table.

    Returns ``{}`` on any failure — no broker session, a rejected IP, a broker
    error — so the table degrades to cached closes per symbol instead of
    blanking. A watchlist is a read-only view; losing live prices must not make
    it unusable, and every affected row is marked ``stale`` so the reader knows.
    """
    try:
        from atr.brokers.iifl.contracts import InstrumentMaster
        from atr.scanner import resolve_conid

        client = _authed_client()
        master = InstrumentMaster(client)
        master.load_cached([exchange.upper()])

        legs: list[tuple[str, Any]] = []
        resolved: list[str] = []
        for symbol in symbols[:50]:
            try:
                legs.append((exchange.upper(), resolve_conid(master, symbol, exchange.upper())))
                resolved.append(symbol)
            except Exception:  # noqa: BLE001 - one unresolved symbol is not fatal
                continue
        if not legs:
            return {}

        payload = client.market_quotes(legs)
        if _broker_error(payload):
            return {}
        rows = _broker_rows(payload)
        return {
            symbol: dict(row)
            for symbol, row in zip(resolved, rows, strict=False)
            if isinstance(row, dict)
        }
    except Exception as exc:  # noqa: BLE001 - live data is optional
        logger.debug("live quote overlay unavailable: %s", exc)
        return {}


@router.websocket("/ws/ticks")
async def ws_ticks(websocket: WebSocket) -> None:
    """Real-time market ticks stream over WebSocket."""
    import json

    from atr.api.stream import get_broadcaster

    # Browsers apply no CORS to websockets, so without this any page the operator
    # visits could open ws://localhost:8000/ws/ticks and drive the broker feed.
    from urllib.parse import urlparse

    from atr.api.middleware import _ALLOWED_ORIGINS

    origin = websocket.headers.get("origin")
    if origin and origin not in _ALLOWED_ORIGINS:
        host = (websocket.headers.get("host") or "").lower()
        if urlparse(origin).netloc.lower() != host:
            await websocket.close(code=1008)
            return

    broadcaster = get_broadcaster()
    try:
        broadcaster.set_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass

    await broadcaster.connect(websocket)
    try:
        while True:
            text = await websocket.receive_text()
            try:
                msg = json.loads(text)
                action = msg.get("action")
                symbols = msg.get("symbols", [])
                exchange = msg.get("exchange", "NSEEQ")
                if action == "subscribe" and isinstance(symbols, list):
                    await broadcaster.subscribe(websocket, symbols, exchange)
                elif action == "unsubscribe" and isinstance(symbols, list):
                    await broadcaster.unsubscribe(websocket, symbols)
            except Exception:
                pass
    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)
    except Exception:
        broadcaster.disconnect(websocket)


@router.get("/ticks/history")
def get_tick_history(symbol: str = Query(..., description="Stock symbol"), limit: int = Query(100, ge=1, le=5000)) -> list[dict[str, Any]]:
    """Fetch raw buffered ticks from the in-memory time-series ring buffer."""
    from atr.api.stream import get_broadcaster
    return get_broadcaster().get_tick_history(symbol, limit=limit)


@router.get("/ticks/vwap")
def get_tick_vwap(symbol: str = Query(..., description="Stock symbol"), window: int = Query(900, ge=1, le=86400)) -> dict[str, Any]:
    """Compute instant rolling VWAP over `window` seconds from in-memory ring buffer."""
    from atr.api.stream import get_broadcaster
    return get_broadcaster().get_rolling_vwap(symbol, window_seconds=window)


@router.get("/ticks/candles")
def get_tick_candles(symbol: str = Query(..., description="Stock symbol"), interval: int = 5) -> list[dict[str, Any]]:
    """Resample buffered ticks into sub-minute OHLCV candles via Polars."""
    from atr.api.stream import get_broadcaster
    return get_broadcaster().get_tick_candles(symbol, interval_seconds=interval)
