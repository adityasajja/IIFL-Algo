"""Execution service: the one path from an intent to a venue.

Why this module exists
----------------------
Before it, three code paths built broker orders by hand — the manual order route,
the signal-execute route, and the live runner — and each of them got to decide
what an order was. Two of the three were wrong in ways that only show up at the
exchange:

* ``api/main.py:662`` passed ``OrderType.SL_MARKET``, which **does not exist** on
  the enum. The signal-execute route therefore placed its *entry* order and then
  raised ``AttributeError`` on the stop-loss leg, returning a 500 and leaving a
  live position with no stop and no target.
* Even had the name existed, the leg set ``limit_price`` and a
  ``broker_params["triggerPrice"]`` that nothing reads. ``IiflBroker`` takes the
  trigger from ``order.stop_price``, so the stop would have gone to the exchange
  with no trigger price.

Both are the same failure: an order assembled at the call site, where nothing
checks it. So orders are assembled here, once, and the venue is a parameter.

The venue abstraction is also what makes ``BACKTEST → PAPER → LIVE`` real: the
same :class:`~atr.services.orders.OrderService` lifecycle and the same rule layer,
with a different :class:`Venue`. A backtest fills from bars, paper fills from the
live quote with slippage, live transmits. Three venues, one strategy definition.

What this service refuses to do
-------------------------------
**It will not re-submit a duplicate.** If ``open_order`` reports that the intent
already has an order, this returns that order untouched. Re-submitting on a retry
is exactly the double-order the idempotency guard exists to prevent, and a guard
that is bypassed by the retry path guards nothing.

**It will not invent an outcome when the venue call fails.** If the broker call
raises, the order is left at ``SUBMITTED`` — which is the truth: the request was
sent and we do not know whether it landed. Writing a rejection there would be a
lie, and writing a fill would be a worse one. Reconciliation resolves unknowns;
this service records them as unknown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from atr.execution.oms import OrderDraft
from atr.services.orders import OrderService

logger = logging.getLogger("atr.services.execution")

#: Venue outcomes. Deliberately not the OMS states — a venue reports what the
#: *exchange* said, and mapping that onto our lifecycle is this module's job.
VENUE_ACCEPTED = "ACCEPTED"
VENUE_PARTIAL = "PARTIAL"
VENUE_FILLED = "FILLED"
VENUE_REJECTED = "REJECTED"

VENUE_STATUSES = frozenset({VENUE_ACCEPTED, VENUE_PARTIAL, VENUE_FILLED, VENUE_REJECTED})


class VenueError(RuntimeError):
    """The venue call failed and the outcome is unknown.

    Carries the ``order_id`` because the only useful thing to tell an operator is
    *which* order is now in an unknown state. The order stays at ``SUBMITTED``.
    """

    def __init__(self, message: str, *, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(message)


@dataclass(frozen=True)
class VenueOutcome:
    """What a venue said about one submission."""

    status: str
    broker_order_id: str | None = None
    filled_qty: float = 0.0
    filled_price: float | None = None
    #: The frictions actually charged, in rupees. Carried through to the event log
    #: rather than recomputed later, so a paper account's net P&L is a fact about
    #: what happened and not a function of today's cost model.
    commission: float = 0.0
    reject_reason: str | None = None
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in VENUE_STATUSES:
            raise ValueError(
                f"venue status must be one of {sorted(VENUE_STATUSES)}, got {self.status!r}"
            )
        if self.status == VENUE_REJECTED and not (self.reject_reason or "").strip():
            # A rejection with no reason cannot be acted on, and it is the one
            # outcome where the reason is the whole value of the message.
            raise ValueError("a venue rejection must carry a reason")

    @property
    def filled(self) -> bool:
        return self.status in (VENUE_FILLED, VENUE_PARTIAL)


class Venue(Protocol):
    """Somewhere an order can be sent. Backtest, paper and live all satisfy this."""

    def submit(self, draft: OrderDraft) -> VenueOutcome: ...


@dataclass(frozen=True)
class PlacementResult:
    """The outcome of one :meth:`ExecutionService.place` call."""

    order_id: str
    status: str
    created: bool
    broker_order_id: str | None = None
    filled_quantity: float = 0.0
    reject_reason: str | None = None
    duplicate_of: str | None = None
    order: dict[str, Any] = field(default_factory=dict)

    @property
    def duplicate(self) -> bool:
        return not self.created


@dataclass
class ExecutionService:
    """Drives one intent through the OMS and out to a venue."""

    orders: OrderService
    venue: Venue

    def place(
        self,
        draft: OrderDraft,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> PlacementResult:
        """Create, risk-check, submit, transmit — in that order, once.

        The order of operations is the safety property. Risk is evaluated before
        anything is transmitted, and the ``SUBMITTED`` event is written before the
        venue call so that a crash mid-transmission leaves a record that the
        request went out rather than no record at all.
        """
        opened = self.orders.open_order(
            draft, idempotency_key=idempotency_key, request_id=request_id
        )
        order_id = opened.order_id

        if not opened.created:
            existing = self.orders.get(order_id, draft.user_id) or {}
            logger.info(
                "duplicate intent for %s %s: returning order %s at %s without resubmitting",
                draft.side,
                draft.symbol,
                order_id,
                existing.get("status"),
            )
            return self._result(
                order_id, existing, created=False, duplicate_of=opened.duplicate_of
            )

        status = self.orders.validate(order_id, draft.user_id)
        if status != "RISK_APPROVED":
            logger.warning("order %s refused by risk", order_id)
            return self._result(
                order_id, self.orders.get(order_id, draft.user_id) or {}, created=True
            )

        self.orders.submit(order_id, draft.user_id, request_id=request_id)

        try:
            outcome = self.venue.submit(draft)
        except Exception as exc:  # noqa: BLE001 - the order stays SUBMITTED, deliberately
            logger.exception("venue submission failed for order %s", order_id)
            raise VenueError(
                f"the venue call failed for order {order_id}; the order is SUBMITTED "
                f"and its state at the exchange is unknown ({exc})",
                order_id=order_id,
            ) from exc

        self._apply(order_id, draft.user_id, outcome, request_id=request_id)
        return self._result(
            order_id, self.orders.get(order_id, draft.user_id) or {}, created=True
        )

    def _apply(
        self,
        order_id: str,
        user_id: str,
        outcome: VenueOutcome,
        *,
        request_id: str | None,
    ) -> None:
        """Fold the venue's answer into the lifecycle.

        A fill is recorded *after* the acknowledgement, because that is the order
        the exchange reports them in and because ``ACKNOWLEDGED`` is what makes a
        fill a legal transition from a state we actually passed through.
        """
        if outcome.status == VENUE_REJECTED:
            self.orders.reject(
                order_id,
                user_id,
                reason=outcome.reject_reason or "rejected by the venue",
                source="broker",
                raw=outcome.raw,
                request_id=request_id,
            )
            return

        self.orders.acknowledge(
            order_id,
            user_id,
            broker_order_id=outcome.broker_order_id,
            raw=outcome.raw,
            request_id=request_id,
        )
        if outcome.filled and outcome.filled_qty > 0:
            self.orders.record_fill(
                order_id,
                user_id,
                filled_qty=outcome.filled_qty,
                filled_price=outcome.filled_price or draft_reference_price(outcome),
                commission=outcome.commission,
                raw=outcome.raw,
                request_id=request_id,
            )

    def _result(
        self,
        order_id: str,
        order: dict[str, Any],
        *,
        created: bool,
        duplicate_of: str | None = None,
    ) -> PlacementResult:
        return PlacementResult(
            order_id=order_id,
            status=str(order.get("status") or "UNKNOWN"),
            created=created,
            broker_order_id=order.get("broker_order_id"),
            filled_quantity=float(order.get("filled_quantity") or 0.0),
            reject_reason=order.get("reject_reason"),
            duplicate_of=duplicate_of,
            order=order,
        )


def draft_reference_price(outcome: VenueOutcome) -> float:
    """A fill price we can defend, or a refusal.

    A fill with no price cannot be recorded: ``orders.avg_fill_price`` would be
    zero and every P&L figure downstream would be wrong in a way nobody could
    trace back to here. Better to fail loudly than to write a zero.
    """
    if outcome.filled_price:
        return float(outcome.filled_price)
    raise ValueError(
        "a filled venue outcome must carry a price — refusing to record a fill at zero"
    )


# ===========================================================================
# Venues
# ===========================================================================
def outcome_from_broker_order(placed: Any) -> VenueOutcome:
    """Map a core :class:`~atr.core.models.Order` from a broker onto a venue outcome.

    One place, so the ``OrderStatus`` → lifecycle mapping cannot drift between the
    manual route and the signal route — which is how the ``SL_MARKET`` bug
    survived: each caller decided for itself what the broker's answer meant.
    """
    from atr.core.enums import OrderStatus

    status = placed.status
    if status is OrderStatus.REJECTED:
        return VenueOutcome(
            status=VENUE_REJECTED,
            broker_order_id=placed.broker_order_id,
            reject_reason=placed.reject_reason or "rejected by the broker",
            raw={"status": status.value, "reason": placed.reject_reason},
        )
    if status is OrderStatus.FILLED:
        return VenueOutcome(
            status=VENUE_FILLED,
            broker_order_id=placed.broker_order_id,
            filled_qty=float(placed.filled_quantity),
            filled_price=float(placed.avg_fill_price) or None,
            raw={"status": status.value},
        )
    if status is OrderStatus.PARTIALLY_FILLED:
        return VenueOutcome(
            status=VENUE_PARTIAL,
            broker_order_id=placed.broker_order_id,
            filled_qty=float(placed.filled_quantity),
            filled_price=float(placed.avg_fill_price) or None,
            raw={"status": status.value},
        )
    return VenueOutcome(
        status=VENUE_ACCEPTED,
        broker_order_id=placed.broker_order_id,
        raw={"status": status.value},
    )


#: Order types the execution layer accepts, and what they mean.
#:
#: The canonicalisation itself lives on :meth:`atr.core.enums.OrderType.parse`, so
#: the write path and the venue cannot disagree about what `SL-M` means. It is
#: spelled out there rather than here because the persistence layer needs it too —
#: and because that is where the ``OrderType.SL_MARKET`` bug belongs: the
#: signal-execute route used a member that did not exist on the *second* leg of a
#: three-leg bracket, so the entry order had already gone to the exchange when the
#: ``AttributeError`` fired.
def resolve_order_type(name: str) -> str:
    """Canonical order-type name, or a refusal. Delegates to the enum."""
    from atr.core.enums import OrderType

    return OrderType.parse(name).value


@dataclass
class BrokerVenue:
    """A :class:`Venue` over the existing :class:`~atr.brokers.base.Broker` ABC.

    The broker is used unchanged. The only work here is building the core
    ``Order`` from a draft — and doing it in one place is the entire reason this
    class exists rather than each route constructing its own.
    """

    broker: Any  # atr.brokers.base.Broker
    instruments: Any | None = None  # optional override for tests

    def submit(self, draft: OrderDraft) -> VenueOutcome:
        from atr.core.enums import OrderType, Side
        from atr.core.models import Order

        order_type = OrderType.parse(draft.order_type)
        instrument = self._instrument(draft)
        params: dict[str, Any] = {}
        if draft.product:
            params["product"] = draft.product

        order = Order(
            instrument=instrument,
            side=Side.BUY if draft.side == "BUY" else Side.SELL,
            quantity=abs(float(draft.quantity)),
            order_type=order_type,
            limit_price=draft.limit_price,
            # The trigger lives here, not in `broker_params`: `IiflBroker` reads
            # `order.stop_price` and nothing else.
            stop_price=draft.stop_price,
            tag=draft.tag,
            broker_params=params,
        )
        return outcome_from_broker_order(self.broker.place_order(order))

    def _instrument(self, draft: OrderDraft) -> Any:
        if self.instruments is not None:
            return self.instruments(draft.symbol, draft.exchange)
        master = getattr(self.broker, "master", None)
        if master is None:
            raise VenueError(
                "the broker exposes no instrument master, so the order cannot be built",
                order_id="",
            )
        return master.find(draft.symbol, draft.exchange)


def iifl_venue(broker: Any) -> BrokerVenue:
    """A venue over a live ``IiflBroker``."""
    return BrokerVenue(broker=broker)


class BrokerPortfolio:
    """A portfolio view over a broker's own positions, for the risk engine.

    ``RiskEngine`` needs ``position(symbol)`` and the aggregates. In live trading
    the broker *is* the source of truth for positions, so this reads them rather
    than keeping a second copy in the platform — two position books that can
    disagree is precisely what reconciliation exists to detect, and creating one
    on purpose would be a poor way to start.

    **``equity`` is reported as 0.0 and that is a real limitation, not a value.**
    ``IiflBroker`` exposes positions and per-order margin but no funds endpoint, so
    the platform does not know the account's equity. A ``max_daily_loss`` limit is
    measured from equity, so on the live path that one limit cannot fire. It is
    reported as zero rather than as a guess because a guess here would silently
    switch a safety limit on or off. Adding ``funds()`` to the ``Broker`` ABC is
    tracked in the gap report; until then this class says so out loud.
    """

    def __init__(self, broker: Any) -> None:
        self._broker = broker
        self._by_symbol: dict[str, Any] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        try:
            for position in self._broker.positions():
                self._by_symbol[position.instrument.symbol] = position
        except Exception:  # noqa: BLE001 - an unreadable book is "no positions known"
            logger.exception("could not read positions from the broker")
        self._loaded = True

    def position(self, symbol: str) -> Any:
        self._load()
        found = self._by_symbol.get(symbol)
        if found is not None:
            return found
        return _FlatPosition()

    @property
    def positions(self) -> dict[str, Any]:
        self._load()
        return dict(self._by_symbol)

    @property
    def gross_exposure(self) -> float:
        self._load()
        return sum(abs(p.market_value) for p in self._by_symbol.values())

    @property
    def equity(self) -> float:
        return 0.0


class _FlatPosition:
    """No position in this symbol. The shape ``RiskEngine`` expects."""

    quantity = 0.0
    avg_price = 0.0
    last_price = 0.0

    @property
    def is_flat(self) -> bool:
        return True


__all__ = [
    "VENUE_ACCEPTED",
    "VENUE_FILLED",
    "VENUE_PARTIAL",
    "VENUE_REJECTED",
    "VENUE_STATUSES",
    "BrokerPortfolio",
    "BrokerVenue",
    "ExecutionService",
    "PlacementResult",
    "Venue",
    "VenueError",
    "VenueOutcome",
    "iifl_venue",
    "outcome_from_broker_order",
    "resolve_order_type",
]
