"""Broker-agnostic execution interface.

The backtest engine never imports a concrete broker; live trading swaps
:class:`SimulatedBroker` for an implementation of :class:`Broker`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from atr.core.models import Fill, Instrument, Order, Position


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    @abstractmethod
    def place_order(self, order: Order) -> Order:
        """Submit an order. Returns the order with ``broker_order_id`` set."""

    @abstractmethod
    def modify_order(self, broker_order_id: str, **fields) -> Order:
        ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        ...

    @abstractmethod
    def positions(self) -> list[Position]:
        ...

    @abstractmethod
    def open_orders(self) -> list[Order]:
        ...

    @abstractmethod
    def last_price(self, instruments: list[Instrument]) -> dict[str, float]:
        ...

    def cancel_all(self) -> int:
        cancelled = 0
        for order in self.open_orders():
            if order.broker_order_id:
                self.cancel_order(order.broker_order_id)
                cancelled += 1
        return cancelled


class FillListener(ABC):
    """Receives execution reports (live trading)."""

    @abstractmethod
    def on_fill(self, fill: Fill) -> None: ...

    @abstractmethod
    def on_order_update(self, order: Order) -> None: ...
