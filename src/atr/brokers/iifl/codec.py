"""Binary decoders for the IIFL market-data bridge.

All market events arrive as fixed-length binary payloads over MQTT. Prices are
integers that must be divided by the ``priceDivisor`` field carried in the
market-feed packet itself (the docs' own example does exactly this).

Reference: https://developers.iiflcapital.com/apidocs/marketdatastream
"""

from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime


class Depth(ctypes.Structure):
    _pack_ = 2
    _fields_ = [
        ("quantity", ctypes.c_uint32),
        ("price", ctypes.c_int32),
        ("orders", ctypes.c_int16),
        ("transactionType", ctypes.c_int16),
    ]


class MarketFeedPacket(ctypes.Structure):
    """186-byte market watch packet (the wire frame is 188 bytes)."""

    _pack_ = 2
    _fields_ = [
        ("ltp", ctypes.c_int32),
        ("lastTradedQuantity", ctypes.c_uint32),
        ("tradedVolume", ctypes.c_uint32),
        ("high", ctypes.c_int32),
        ("low", ctypes.c_int32),
        ("open", ctypes.c_int32),
        ("close", ctypes.c_int32),
        ("averageTradedPrice", ctypes.c_int32),
        ("reserved", ctypes.c_uint16),
        ("bestBidQuantity", ctypes.c_uint32),
        ("bestBidPrice", ctypes.c_int32),
        ("bestAskQuantity", ctypes.c_uint32),
        ("bestAskPrice", ctypes.c_int32),
        ("totalBidQuantity", ctypes.c_uint32),
        ("totalAskQuantity", ctypes.c_uint32),
        ("priceDivisor", ctypes.c_int32),
        ("lastTradedTime", ctypes.c_int32),
        ("marketDepth", Depth * 10),
    ]


FEED_PACKET_BYTES = ctypes.sizeof(MarketFeedPacket)  # 186


@dataclass(slots=True)
class DepthLevel:
    price: float
    quantity: int
    orders: int
    transaction_type: int


@dataclass(slots=True)
class MarketFeed:
    ltp: float
    last_traded_quantity: int
    traded_volume: int
    high: float
    low: float
    open: float
    close: float
    average_traded_price: float
    best_bid_price: float
    best_bid_quantity: int
    best_ask_price: float
    best_ask_quantity: int
    total_bid_quantity: int
    total_ask_quantity: int
    last_traded_time: datetime
    depth: list[DepthLevel] = field(default_factory=list)

    @property
    def mid(self) -> float:
        if self.best_bid_price and self.best_ask_price:
            return (self.best_bid_price + self.best_ask_price) / 2
        return self.ltp


def decode_market_feed(data: bytes | bytearray) -> MarketFeed:
    """Decode a market-feed payload into prices in rupees."""
    raw = bytes(data)[:FEED_PACKET_BYTES]
    if len(raw) < FEED_PACKET_BYTES:
        raise ValueError(f"market feed packet too short: {len(raw)} < {FEED_PACKET_BYTES}")
    pkt = MarketFeedPacket.from_buffer_copy(raw)
    div = pkt.priceDivisor or 100

    depth = [
        DepthLevel(
            price=level.price / div,
            quantity=level.quantity,
            orders=level.orders,
            transaction_type=level.transactionType,
        )
        for level in pkt.marketDepth
    ]
    return MarketFeed(
        ltp=pkt.ltp / div,
        last_traded_quantity=pkt.lastTradedQuantity,
        traded_volume=pkt.tradedVolume,
        high=pkt.high / div,
        low=pkt.low / div,
        open=pkt.open / div,
        close=pkt.close / div,
        average_traded_price=pkt.averageTradedPrice / div,
        best_bid_price=pkt.bestBidPrice / div,
        best_bid_quantity=pkt.bestBidQuantity,
        best_ask_price=pkt.bestAskPrice / div,
        best_ask_quantity=pkt.bestAskQuantity,
        total_bid_quantity=pkt.totalBidQuantity,
        total_ask_quantity=pkt.totalAskQuantity,
        last_traded_time=datetime.fromtimestamp(pkt.lastTradedTime, tz=UTC),
        depth=depth,
    )


def decode_open_interest(data: bytes | bytearray) -> dict[str, int]:
    oi, day_high, day_low, prev = struct.unpack("iiii", bytes(data)[:16])
    return {
        "openInterest": oi,
        "dayHighOi": day_high,
        "dayLowOi": day_low,
        "previousOi": prev,
    }


def decode_lpp(data: bytes | bytearray) -> dict[str, float]:
    high, low, divisor = struct.unpack("IIi", bytes(data)[:12])
    div = divisor or 100
    return {"lppHigh": high / div, "lppLow": low / div, "priceDivisor": divisor}


def decode_circuit(data: bytes | bytearray, key: str) -> dict[str, float]:
    instrument_id, value, divisor = struct.unpack("IIi", bytes(data)[:12])
    div = divisor or 100
    return {"instrumentId": instrument_id, key: value / div, "priceDivisor": divisor}


def decode_market_status(data: bytes | bytearray) -> dict[str, int]:
    (code,) = struct.unpack("H", bytes(data)[:2])
    return {"marketStatusCode": code}
