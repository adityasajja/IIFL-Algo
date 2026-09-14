"""Order service: the transaction-owning half of the OMS.

:mod:`atr.execution.oms` defines the state machine — the states, the legal
transitions, the fill arithmetic — and knows nothing about a database, because it
is a compute layer. This module *applies* that machine to persisted orders, which
means it owns the session and therefore lives in ``atr.services``.

The split is not decoration. It is what keeps the transition table testable
without a database and what stops a strategy module from being able to write to
the order store.

What this service is the only writer of
---------------------------------------
``orders.status``. There is no setter on the repository and no route writes the
column; every change goes through :meth:`OrderService._advance`, which asserts
the transition, appends the event, and lets the repository reproject. So the
invariant "``orders.status`` equals the newest event's ``to_status``" cannot be
broken by a caller — only by a bug in one method.

The risk check is *inside* the transition to ``RISK_APPROVED``
-------------------------------------------------------------
This is the fix for the defect where ``RiskEngine`` existed but no route
referenced it. :meth:`validate` moves ``NEW → VALIDATING``, then asks the gate,
then moves to ``RISK_APPROVED`` or ``REJECTED`` — all in one transaction. There
is no method that reaches ``RISK_APPROVED`` without a decision, so no route can
put an order in front of a broker having skipped the check.

If the gate *raises*, the transaction rolls back and the order stays at ``NEW``:
half a validation is not a state, and the check that guards submission fails
closed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from atr.appdb.engine import AppDatabase, utcnow
from atr.appdb.repositories import (
    OrderEventRepository,
    OrderIntentRepository,
    OrderRepository,
)
from atr.execution.oms import (
    EVENT_SOURCES,
    OrderDraft,
    RiskGate,
    assert_transition,
    elapsed_ms,
    fill_status,
    slippage_bps,
)

logger = logging.getLogger("atr.services.orders")


class OrderNotFound(LookupError):
    """No such order *for this user*. Missing and not-yours are the same answer.

    Deliberately one exception for both: the API must answer 404 rather than 403,
    and a distinct "exists but not yours" error is exactly the kind of thing that
    leaks an id's existence into a response.
    """


@dataclass(frozen=True)
class OpenedOrder:
    """The result of :meth:`OrderService.open_order`."""

    order_id: str
    created: bool
    #: Set when ``created`` is False: the order that already owns the intent.
    duplicate_of: str | None = None


class _LostIdempotencyRace(Exception):
    """Internal: unwinds the savepoint that had already inserted the order row."""

    def __init__(self, winner_order_id: str) -> None:
        self.winner_order_id = winner_order_id
        super().__init__(winner_order_id)


@dataclass
class OrderService:
    """Applies the order state machine to persisted orders."""

    db: AppDatabase
    risk_gate: RiskGate
    clock: Callable[[], datetime] = field(default=utcnow)

    # ------------------------------------------------------------------ reads
    def get(self, order_id: str, user_id: str) -> dict[str, Any] | None:
        with self.db.session() as session:
            return OrderRepository.get(session, order_id, user_id)

    def history(self, order_id: str, user_id: str) -> list[dict[str, Any]]:
        """The full event log, oldest first. Raises if the order is not yours."""
        with self.db.session() as session:
            if OrderRepository.get(session, order_id, user_id) is None:
                raise OrderNotFound(order_id)
            return OrderEventRepository.history(session, order_id)

    def list_orders(
        self, user_id: str, **filters: Any
    ) -> tuple[list[dict[str, Any]], int]:
        with self.db.session() as session:
            return OrderRepository.list_for_user(session, user_id, **filters)

    def open_orders(self, user_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            return OrderRepository.open_orders(session, user_id)

    def fill_history(self, order_id: str, user_id: str) -> list[dict[str, Any]]:
        """Only the execution events — the fills, not the state changes."""
        with self.db.session() as session:
            if OrderRepository.get(session, order_id, user_id) is None:
                raise OrderNotFound(order_id)
            return OrderEventRepository.fills(session, order_id)

    # ------------------------------------------------------------------ open
    def open_order(
        self,
        draft: OrderDraft,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> OpenedOrder:
        """Create the order at ``NEW``, or return the one that already exists.

        With an ``idempotency_key`` this is the durable duplicate guard, and the
        insert *is* the guard — there is no check-then-act window. The order row
        and the key claim are written inside one savepoint: if the claim loses a
        race, the savepoint is rolled back and the briefly-inserted order never
        becomes visible.

        That rollback is not tidiness. A stray ``NEW`` order would sit in
        ``open_orders()`` forever and reconciliation would report a mismatch that
        the platform itself created.
        """
        try:
            with self.db.session() as session, session.begin_nested():
                order = OrderRepository.create(
                    session,
                    user_id=draft.user_id,
                    symbol=draft.symbol,
                    side=draft.side,
                    quantity=draft.quantity,
                    mode=draft.mode,
                    exchange=draft.exchange,
                    asset_class=draft.asset_class,
                    order_type=draft.order_type,
                    limit_price=draft.limit_price,
                    stop_price=draft.stop_price,
                    tif=draft.tif,
                    product=draft.product,
                    requested_price=draft.requested_price,
                    deployment_id=draft.deployment_id,
                    strategy_id=draft.strategy_id,
                    strategy_version=draft.strategy_version,
                    signal_id=draft.signal_id,
                    correlation_id=draft.correlation_id,
                    tag=draft.tag,
                    request_id=request_id,
                )
                order_id = order["order_id"]
                if idempotency_key:
                    winner, created = OrderIntentRepository.create_if_absent(
                        session,
                        idempotency_key=idempotency_key,
                        order_id=order_id,
                        user_id=draft.user_id,
                        strategy_id=draft.strategy_id,
                        strategy_version=draft.strategy_version,
                        signal_id=draft.signal_id,
                    )
                    if not created:
                        raise _LostIdempotencyRace(winner)
                return OpenedOrder(order_id=order_id, created=True)
        except _LostIdempotencyRace as lost:
            logger.info(
                "duplicate order intent suppressed; returning order %s",
                lost.winner_order_id,
            )
            return OpenedOrder(
                order_id=lost.winner_order_id,
                created=False,
                duplicate_of=lost.winner_order_id,
            )

    # ------------------------------------------------------------- validation
    def validate(
        self, order_id: str, user_id: str, *, gate: RiskGate | None = None
    ) -> str:
        """``NEW`` → ``VALIDATING`` → ``RISK_APPROVED`` | ``REJECTED``.

        The only path to ``RISK_APPROVED``. See the module docstring.
        """
        evaluate = gate or self.risk_gate
        with self.db.session() as session:
            row = OrderRepository.get(session, order_id, user_id)
            if row is None:
                raise OrderNotFound(order_id)
            assert_transition(row["status"], "VALIDATING", order_id)
            OrderEventRepository.append(
                session,
                order_id=order_id,
                from_status=row["status"],
                to_status="VALIDATING",
                ts=self._now(),
                source="oms",
            )
            decision = evaluate(OrderDraft.from_row(row))
            if decision.allowed:
                OrderEventRepository.append(
                    session,
                    order_id=order_id,
                    from_status="VALIDATING",
                    to_status="RISK_APPROVED",
                    ts=self._now(),
                    source="risk",
                )
                return "RISK_APPROVED"
            logger.info("order %s rejected by risk: %s", order_id, decision.reason)
            OrderEventRepository.append(
                session,
                order_id=order_id,
                from_status="VALIDATING",
                to_status="REJECTED",
                ts=self._now(),
                reject_reason=decision.reason or "rejected by risk",
                raw={"code": decision.code} if decision.code else None,
                source="risk",
            )
            return "REJECTED"

    # -------------------------------------------------------------- submission
    def submit(
        self,
        order_id: str,
        user_id: str,
        *,
        broker_order_id: str | None = None,
        latency_ms: int | None = None,
        request_id: str | None = None,
    ) -> str:
        """``RISK_APPROVED`` → ``SUBMITTED``. Call immediately before transmitting."""
        return self._advance(
            order_id,
            user_id,
            "SUBMITTED",
            broker_order_id=broker_order_id,
            latency_ms=latency_ms,
            request_id=request_id,
            source="oms",
        )

    def acknowledge(
        self,
        order_id: str,
        user_id: str,
        *,
        broker_order_id: str | None = None,
        broker_ts: datetime | None = None,
        raw: dict[str, Any] | str | None = None,
        latency_ms: int | None = None,
        request_id: str | None = None,
    ) -> str:
        """``SUBMITTED`` → ``ACKNOWLEDGED``, recording the broker's own timestamp.

        ``latency_ms`` defaults to *measured* — the gap between the ``SUBMITTED``
        event and now. A latency figure that is only recorded when someone
        remembers to pass one is not a measurement.
        """
        with self.db.session() as session:
            row = OrderRepository.get(session, order_id, user_id)
            if row is None:
                raise OrderNotFound(order_id)
            if latency_ms is None:
                previous = OrderEventRepository.latest(session, order_id)
                if previous is not None and previous["to_status"] == "SUBMITTED":
                    latency_ms = elapsed_ms(previous["ts"], self._now())
            return self._advance_in_session(
                session,
                row,
                "ACKNOWLEDGED",
                broker_order_id=broker_order_id,
                broker_ts=broker_ts,
                ack_ts=self._now(),
                raw=raw,
                latency_ms=latency_ms,
                request_id=request_id,
                source="broker",
            )

    def record_fill(
        self,
        order_id: str,
        user_id: str,
        *,
        filled_qty: float,
        filled_price: float,
        fill_ts: datetime | None = None,
        slippage: float | None = None,
        commission: float | None = None,
        latency_ms: int | None = None,
        raw: dict[str, Any] | str | None = None,
        request_id: str | None = None,
    ) -> str:
        """Record an execution report, advancing to ``PARTIALLY_FILLED`` or ``FILLED``.

        ``filled_qty`` is the **cumulative** quantity the broker reports, matching
        the convention in ``order_events``. Two checks are enforced here because
        both are silent-corruption bugs otherwise:

        * a cumulative figure that *decreases* means the reports arrived out of
          order or a fill was counted twice — refused rather than absorbed;
        * a cumulative figure above the order quantity means the position is about
          to be wrong in a way no downstream reconciliation can attribute.

        ``slippage`` is computed from the order's ``requested_price`` when not
        given, so a fill carries a slippage figure whenever one is derivable.
        """
        with self.db.session() as session:
            row = OrderRepository.get(session, order_id, user_id)
            if row is None:
                raise OrderNotFound(order_id)
            cumulative = float(filled_qty)
            if cumulative <= 0:
                raise ValueError("a fill must be for a positive quantity")
            quantity = float(row["quantity"])
            if cumulative > quantity + 1e-6:
                raise ValueError(
                    f"cumulative fill {cumulative} exceeds order quantity {quantity}"
                )
            if cumulative < float(row["filled_quantity"]) - 1e-6:
                raise ValueError(
                    f"cumulative fill went backwards: {cumulative} < "
                    f"{row['filled_quantity']}"
                )
            if slippage is None:
                slippage = slippage_bps(
                    side=row["side"],
                    requested_price=row["requested_price"],
                    filled_price=filled_price,
                )
            target = fill_status(quantity=quantity, cumulative_filled=cumulative)
            return self._advance_in_session(
                session,
                row,
                target,
                filled_qty=cumulative,
                filled_price=float(filled_price),
                fill_ts=fill_ts or self._now(),
                slippage_bps=slippage,
                commission=commission,
                latency_ms=latency_ms,
                raw=raw,
                request_id=request_id,
                source="broker",
            )

    # ------------------------------------------------------------- cancellation
    def request_cancel(
        self,
        order_id: str,
        user_id: str,
        *,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """Ask the broker to cancel. ``→ CANCEL_PENDING``, never straight to cancelled."""
        return self._advance(
            order_id,
            user_id,
            "CANCEL_PENDING",
            raw={"reason": reason} if reason else None,
            request_id=request_id,
            source="user",
        )

    def confirm_cancel(
        self,
        order_id: str,
        user_id: str,
        *,
        raw: dict[str, Any] | str | None = None,
        request_id: str | None = None,
    ) -> str:
        """The broker confirmed the cancel. ``CANCEL_PENDING`` → ``CANCELLED``."""
        return self._advance(
            order_id,
            user_id,
            "CANCELLED",
            raw=raw,
            request_id=request_id,
            source="broker",
        )

    # ------------------------------------------------------------ other exits
    def reject(
        self,
        order_id: str,
        user_id: str,
        *,
        reason: str,
        source: str = "oms",
        raw: dict[str, Any] | str | None = None,
        request_id: str | None = None,
    ) -> str:
        """Reject. ``reason`` is mandatory — an unexplained rejection is not a record."""
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("rejecting an order requires a reason")
        return self._advance(
            order_id,
            user_id,
            "REJECTED",
            reject_reason=reason,
            raw=raw,
            request_id=request_id,
            source=source,
        )

    def expire(
        self,
        order_id: str,
        user_id: str,
        *,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """The session ended with the order live. ``→ EXPIRED``."""
        return self._advance(
            order_id,
            user_id,
            "EXPIRED",
            raw={"reason": reason} if reason else None,
            request_id=request_id,
            source="oms",
        )

    # ----------------------------------------------------------------- internals
    def _now(self) -> datetime:
        return self.clock()

    def _advance(
        self, order_id: str, user_id: str, to_status: str, **event_fields: Any
    ) -> str:
        with self.db.session() as session:
            row = OrderRepository.get(session, order_id, user_id)
            if row is None:
                raise OrderNotFound(order_id)
            return self._advance_in_session(session, row, to_status, **event_fields)

    def _advance_in_session(
        self, session: Any, row: dict[str, Any], to_status: str, **event_fields: Any
    ) -> str:
        """Assert, append, reproject — in that order, in the caller's transaction."""
        order_id = row["order_id"]
        current = row["status"]
        assert_transition(current, to_status, order_id)
        event_fields.setdefault("ts", self._now())
        source = event_fields.pop("source", "oms")
        if source not in EVENT_SOURCES:
            raise ValueError(f"unknown event source {source!r}")
        OrderEventRepository.append(
            session,
            order_id=order_id,
            from_status=current,
            to_status=to_status,
            source=source,
            **event_fields,
        )
        return to_status


def get_order_service(
    *,
    portfolio: Any = None,
    instruments: Any = None,
    risk: Any = None,
) -> OrderService:
    """Build the default service: durable risk state + the OMS.

    A factory rather than a singleton because the risk gate is *per-request*: it
    is built from the current kill switch, so engaging the switch takes effect on
    the next order rather than on the next restart. Caching it would reintroduce
    exactly the staleness the durable state was meant to remove.

    ``portfolio`` and ``instruments`` are injectable because only the caller knows
    whether it is the paper engine or the live broker asking. The defaults are
    deliberately conservative and are documented as a known limitation: with no
    position store yet, the default portfolio is flat, so the position-level
    limits (``max_position_per_symbol``, ``max_open_positions``, the gross
    exposure check) cannot bite. Every limit that does not need a position —
    kill switch, allowed symbols, order notional, daily trade count — is enforced.
    """
    from atr.services.risk import RiskStateService

    risk = risk or RiskStateService()
    return OrderService(
        db=risk.db,
        risk_gate=risk.gate(
            portfolio=portfolio if portfolio is not None else _FlatPortfolio(),
            instruments=instruments if instruments is not None else _default_instruments(),
        ),
    )


class _FlatPortfolio:
    """A portfolio with no positions, for the limits that need one to exist.

    Named rather than inlined so the limitation is greppable: this is the stand-in
    until the paper engine owns a real position book.
    """

    equity = 0.0
    gross_exposure = 0.0

    def position(self, symbol: str) -> Any:  # noqa: ARG002 - duck-typed interface
        return _FlatPosition()


class _FlatPosition:
    quantity = 0.0
    last_price = 0.0
    avg_price = 0.0


def _default_instruments() -> Any:
    """Resolve an instrument through the master, for the notional arithmetic."""
    from atr.instruments.service import get_instrument_master

    def lookup(symbol: str, exchange: str) -> Any:
        from atr.core.models import Instrument

        master = get_instrument_master()
        try:
            found = master.find(symbol, exchange)
        except Exception:  # noqa: BLE001 - an unknown symbol still needs a shape
            found = None
        if found is None:
            return Instrument(symbol=symbol, exchange=exchange, multiplier=1.0)
        return found

    return lookup


__all__ = ["OpenedOrder", "OrderNotFound", "OrderService", "get_order_service"]
