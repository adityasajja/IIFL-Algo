"""Event types and a minimal synchronous event bus.

The bus is deliberately simple: the backtest loop is single-threaded and
deterministic, and an async bus there would buy nothing but nondeterminism.
Live components can reuse the same event types and push them onto asyncio
queues if they need fan-out.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from atr.core.models import Bar, Fill, Instrument, Order, Position

# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Event:
    ts: datetime
    name: str = "event"


@dataclass(slots=True)
class MarketEvent(Event):
    bars: dict[str, Bar] = field(default_factory=dict)
    name: str = "market"

    @classmethod
    def from_snapshot(cls, ts: datetime, bars: dict[str, Bar]) -> MarketEvent:
        return cls(ts=ts, bars=bars)


@dataclass(slots=True)
class SignalEvent(Event):
    instrument: Instrument | None = None
    side: Any = None  # core.enums.Side
    target_quantity: float | None = None
    target_weight: float | None = None
    order_type: Any = None
    limit_price: float | None = None
    stop_price: float | None = None
    tag: str | None = None
    name: str = "signal"


@dataclass(slots=True)
class OrderEvent(Event):
    order: Order | None = None
    name: str = "order"


@dataclass(slots=True)
class FillEvent(Event):
    fill: Fill | None = None
    name: str = "fill"


@dataclass(slots=True)
class RiskEvent(Event):
    message: str = ""
    severity: str = "WARN"  # INFO | WARN | FATAL
    order: Order | None = None
    name: str = "risk"


@dataclass(slots=True)
class SessionEvent(Event):
    phase: str = ""  # START | END | EOD_SQUAREOFF
    name: str = "session"


# --------------------------------------------------------------------------
# Bus
# --------------------------------------------------------------------------


class EventBus:
    """Tiny pub/sub with sync and coroutine handler support."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self, event_name: str, handler: Callable) -> None:
        self._handlers[event_name].append(handler)

    def subscribe_many(self, names: Iterable[str], handler: Callable) -> None:
        for name in names:
            self.subscribe(name, handler)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event: Event) -> list[Any]:
        results: list[Any] = []
        for handler in self._handlers.get(event.name, []):
            result = handler(event)
            if inspect.isawaitable(result):
                if self._loop is None:
                    raise RuntimeError(
                        "coroutine handler registered but no event loop bound; "
                        "call EventBus.bind_loop()"
                    )
                results.append(asyncio.run_coroutine_threadsafe(result, self._loop))
            else:
                results.append(result)
        return results

    def clear(self) -> None:
        self._handlers.clear()


# Convenience helper for logging/human consumption
def describe_position_snapshot(positions: dict[str, Position]) -> str:
    if not positions:
        return "flat"
    parts = [
        f"{sym}:{p.quantity:+.0f}@{p.avg_price:.2f}"
        for sym, p in positions.items()
        if not p.is_flat
    ]
    return " | ".join(parts) if parts else "flat"
