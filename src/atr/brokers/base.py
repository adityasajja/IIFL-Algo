"""Broker-agnostic execution interface.

The backtest engine never imports a concrete broker; live trading swaps
:class:`SimulatedBroker` for an implementation of :class:`Broker`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from atr.core.models import Fill, Funds, Holding, Instrument, Order, Position


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

    # ---------------------------------------------------------------- reconciliation
    def holdings(self) -> list[Holding]:
        """Settled holdings in the demat account.

        Not abstract, and that is deliberate: adding an abstract method to this ABC
        breaks every existing implementer at import time, including the simulated
        broker the backtester uses. Raising here instead means the *reconciler*
        decides what an unsupported comparison means — it records "not comparable"
        with a reason rather than silently reporting a clean run, which is the same
        rule that stops a missing price being read as zero.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not expose holdings, so holdings cannot be reconciled"
        )

    def funds(self) -> Funds:
        """The account's cash position.

        See :meth:`holdings` for why this is not abstract. Without it the
        platform cannot measure equity on the live path, so ``max_daily_loss``
        cannot fire — an absence worth surfacing rather than defaulting away.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not expose funds, so funds cannot be reconciled"
        )

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
