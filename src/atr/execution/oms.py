"""The order state machine — pure, with no I/O.

This module knows the states, the legal transitions, and the arithmetic of a
fill. It deliberately knows nothing about a database: it is a compute layer, and
`tests/test_architecture.py` fails the build if it ever imports ``atr.appdb``.
The transaction-owning use case that *persists* these transitions lives in
:mod:`atr.services.orders`, which is where a caller with a session belongs.

Why the state machine exists at all
-----------------------------------
Before it, an order carried a single mutable ``status`` field that three separate
code paths — the manual order route, the signal-execute route, and the live
runner — each set as they pleased. Nothing could answer the two questions that
matter after something goes wrong: *what happened, in order* and *who changed
it*. ``NEW → FILLED`` and ``NEW → CANCELLED → FILLED`` were both representable,
and both were silent.

Design notes that are load-bearing
----------------------------------
**The machine is data, not a chain of ifs.** :data:`VALID_TRANSITIONS` is the
whole definition and :func:`assert_transition` is its only reader. Adding a state
means editing one table, and a state nobody can reach is visible rather than
implied.

**``PARTIALLY_FILLED`` may transition to itself.** A second partial fill is not a
state *change* but it is an *event*, and refusing it would silently drop a fill —
the position would then disagree with the broker, and reconciliation would report
a mismatch the platform caused itself. It is the only self-loop in the machine.

**``CANCEL_PENDING`` is mandatory.** ``CANCELLED`` is reachable only from it, so
even an immediate broker cancel writes two events: the request and the outcome.
That costs one row and buys the fact that a cancel was *requested*, which is
precisely what is missing when someone asks why a position closed early.

**``ACKNOWLEDGED`` cannot be rejected.** Once the exchange holds the order it can
fill, be cancelled, or expire; a late "rejection" from a broker that already
acknowledged is modelled as a cancel, because that is what actually happened to
the position.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

#: Statuses from which no further transition is legal. Mirrors
#: ``atr.appdb.repositories.TERMINAL_ORDER_STATUSES``; duplicated rather than
#: imported because this layer may not depend on the persistence layer. A test
#: asserts the two sets agree, so the duplication cannot drift.
TERMINAL_STATES: frozenset[str] = frozenset(
    {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
)

ORDER_STATES: tuple[str, ...] = (
    "NEW",
    "VALIDATING",
    "RISK_APPROVED",
    "SUBMITTED",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCEL_PENDING",
    "CANCELLED",
    "REJECTED",
    "EXPIRED",
)

#: Every legal transition, and nothing else.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    # CANCEL_PENDING is reachable from every live state, including the ones where
    # nothing has reached a broker yet. "Cancel" is a request a user can make at
    # any point before the order is done; whether there is anything to send to the
    # exchange is the *broker's* problem, not a reason to refuse the request. The
    # two-step shape (request, then outcome) is what keeps the request on the
    # record even when there is nothing to transmit.
    "NEW": frozenset({"VALIDATING", "REJECTED", "EXPIRED", "CANCEL_PENDING"}),
    "VALIDATING": frozenset({"RISK_APPROVED", "REJECTED", "CANCEL_PENDING"}),
    "RISK_APPROVED": frozenset(
        {"SUBMITTED", "REJECTED", "EXPIRED", "CANCEL_PENDING"}
    ),
    # The broker answers a submission by accepting it or rejecting it.
    "SUBMITTED": frozenset({"ACKNOWLEDGED", "REJECTED", "EXPIRED", "CANCEL_PENDING"}),
    "ACKNOWLEDGED": frozenset(
        {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "EXPIRED"}
    ),
    # The self-loop is the second partial fill. See the module docstring.
    # There is deliberately no direct edge to CANCELLED: a partially filled order
    # is cancelled by the same route as any other, through CANCEL_PENDING.
    "PARTIALLY_FILLED": frozenset(
        {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "EXPIRED"}
    ),
    # A cancel in flight can still be overtaken by one more fill.
    "CANCEL_PENDING": frozenset({"CANCELLED", "PARTIALLY_FILLED", "FILLED"}),
    "FILLED": frozenset(),
    "CANCELLED": frozenset(),
    "REJECTED": frozenset(),
    "EXPIRED": frozenset(),
}

#: Sources recorded on an event, so "who did this" is answerable.
EVENT_SOURCES: frozenset[str] = frozenset(
    {"oms", "risk", "broker", "user", "reconciler"}
)

#: The provenance stamp written to an order's ``NEW`` event by the only path
#: that can honestly write one: the OMS, at the instant it creates the order.
#:
#: This exists because the learning engine has to tell a trade that was raised
#: live from one that was replayed out of history, and **timestamps alone cannot
#: do it**. A backfill harness is free to write historical market timestamps, and
#: a reader has no way to tell that from a record that was genuinely made at the
#: time — which is exactly the distinction the whole evidence vocabulary rests
#: on. So the marker is a fact the live path writes and the replay path does not:
#: an order with no stamp is graded in-sample, conservatively, because it cannot
#: demonstrate it was recorded before its own outcome.
#:
#: ``"forward"`` rather than a module-specific spelling so the value is the same
#: word the learning dataset stores; ``atr.services.learning`` reads it back
#: through ``evidence_grade``.
PROVENANCE_FORWARD = "forward"
#: The key the stamp lives under in the event's ``raw`` payload.
PROVENANCE_KEY = "provenance"
#: The key holding the wall-clock instant the order was created. Kept beside the
#: stamp so a later reader can see *when* the claim was made, not only that it
#: was made.
RECORDED_AT_KEY = "recorded_at"


class InvalidTransition(RuntimeError):
    """An illegal state change was attempted.

    Raised rather than ignored. A silent no-op here would leave the caller
    believing the order moved when it did not, which is the failure mode the
    whole event log exists to prevent.
    """

    def __init__(self, from_status: str, to_status: str, order_id: str = "") -> None:
        self.from_status = from_status
        self.to_status = to_status
        self.order_id = order_id
        suffix = f" (order {order_id})" if order_id else ""
        super().__init__(f"illegal order transition {from_status} -> {to_status}{suffix}")


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATES


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in VALID_TRANSITIONS.get(from_status, frozenset())


def next_states(from_status: str) -> frozenset[str]:
    """The legal destinations from ``from_status``. Empty for a terminal state."""
    return VALID_TRANSITIONS.get(from_status, frozenset())


def assert_transition(from_status: str, to_status: str, order_id: str = "") -> None:
    """Raise :class:`InvalidTransition` unless the move is legal."""
    if to_status not in ORDER_STATES:
        raise InvalidTransition(from_status, f"{to_status} (unknown state)", order_id)
    if not can_transition(from_status, to_status):
        raise InvalidTransition(from_status, to_status, order_id)


def slippage_bps(
    *, side: str, requested_price: float | None, filled_price: float | None
) -> float | None:
    """Slippage in basis points, **positive when the fill is worse than requested**.

    The sign convention is stated because a sign convention nobody writes down is
    a sign convention that gets flipped. For a BUY, paying above the requested
    price is adverse; for a SELL, receiving below it is adverse. Both come out
    positive, so a positive number always means "this cost money".

    Returns ``None`` when either price is missing, rather than ``0.0`` — an
    unmeasurable slippage is not a zero-slippage fill.
    """
    if not requested_price or not filled_price:
        return None
    side = (side or "").strip().upper()
    if side not in ("BUY", "SELL"):
        return None
    raw = (filled_price - requested_price) / requested_price
    if side == "SELL":
        raw = -raw
    return raw * 10_000.0


def fill_status(*, quantity: float, cumulative_filled: float, tol: float = 1e-9) -> str:
    """Whether a cumulative fill completes the order or only advances it."""
    if cumulative_filled >= quantity - tol:
        return "FILLED"
    return "PARTIALLY_FILLED"


def idempotency_key_for(
    *,
    user_id: str,
    strategy_id: str | None,
    strategy_version: int | None,
    signal_id: str | None,
    symbol: str,
    side: str,
    leg_index: int = 0,
) -> str:
    """The stable identity of an *intent to trade*.

    Same signal, same strategy version, same leg → same key → one order, however
    many times the caller retries. It lives here rather than next to the table it
    keys because it is a statement about *intent*, not about storage, and the
    route layer needs it without being allowed to touch the database.

    ``user_id`` is included on top of the tuple in ``docs/DATA_MODEL.md``
    (``strategy_id|strategy_version|signal_id|symbol|side|leg_index``) because a
    strategy may be a *shared registry* entry: two accounts running
    ``engine_key="ema_cross"`` would otherwise produce the same key for the same
    signal, and the second account would be handed the first account's order id.
    Deriving the key from a superset of the documented tuple changes no behaviour
    for a single account and closes that hole.

    **Do not derive a key for a manual order.** With ``strategy_id`` and
    ``signal_id`` both ``None`` this function still returns a stable hash, and
    two unrelated manual orders for the same symbol and side would collide into
    one — a person clicking "buy 10 RELIANCE" twice would get a single order and
    no indication that the second was suppressed. A key means "this is the same
    *intent*", and a manual order has no intent identity to compare. Callers
    should derive one only when there is a signal or a strategy version to
    identify, and otherwise pass ``None`` or an explicit caller-supplied key.
    """
    parts = [
        user_id.strip(),
        (strategy_id or "").strip(),
        "" if strategy_version is None else str(int(strategy_version)),
        (signal_id or "").strip(),
        symbol.strip().upper(),
        side.strip().upper(),
        str(int(leg_index)),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def elapsed_ms(start: datetime, end: datetime) -> int:
    """Whole milliseconds between two naive-UTC timestamps, floored at zero."""
    return max(0, int((end - start).total_seconds() * 1000))


# ===========================================================================
# The order draft
# ===========================================================================
@dataclass
class OrderDraft:
    """Everything needed to raise an order, before it exists.

    A separate type from the persisted row because the two have different jobs:
    the draft is what a caller *intends*, the row is what the platform *recorded*,
    and conflating them is how a caller ends up able to set a status directly.
    """

    user_id: str
    symbol: str
    side: str
    quantity: float
    mode: str = "PAPER"
    exchange: str = "NSEEQ"
    asset_class: str = "EQUITY"
    order_type: str = "MARKET"
    limit_price: float | None = None
    stop_price: float | None = None
    tif: str = "DAY"
    product: str | None = None
    requested_price: float | None = None
    deployment_id: str | None = None
    strategy_id: str | None = None
    strategy_version: int | None = None
    signal_id: str | None = None
    correlation_id: str | None = None
    tag: str | None = None
    #: Why this order exists, in the words of the rule that raised it. Carried on
    #: the draft so it reaches the order's ``NEW`` event, which is the only place
    #: a monitoring screen can read the causal chain from. Without it an order is
    #: anonymous: the fills show *what* happened and nothing shows *why*.
    signal_reason: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> OrderDraft:
        """Rebuild the draft from a persisted order, for the risk gate to read."""
        return cls(
            user_id=row["user_id"],
            symbol=row["symbol"],
            side=row["side"],
            quantity=float(row["quantity"]),
            mode=row["mode"],
            exchange=row["exchange"],
            asset_class=row["asset_class"],
            order_type=row["order_type"],
            limit_price=row["limit_price"],
            stop_price=row["stop_price"],
            tif=row["tif"],
            product=row["product"],
            requested_price=row["requested_price"],
            deployment_id=row["deployment_id"],
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            signal_id=row["signal_id"],
            correlation_id=row["correlation_id"],
            tag=row["tag"],
            # Not persisted on ``orders`` (it lives on the event), so a row
            # rebuilt from the projection has no reason to carry.
            signal_reason=None,
        )


# ===========================================================================
# Risk gate
# ===========================================================================
@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None
    code: str | None = None

    @classmethod
    def ok(cls) -> RiskDecision:
        return cls(True)

    @classmethod
    def reject(cls, reason: str, code: str = "risk_rejected") -> RiskDecision:
        return cls(False, reason, code)


class RiskGate(Protocol):
    """Anything that can answer "may this order be submitted?".

    A plain callable so a test can pass a lambda and production can pass
    :class:`LimitsRiskGate`. The OMS requires one at construction time — there is
    deliberately no default, because a default would be either permissive (a
    footgun) or rejecting (an outage), and neither should be chosen silently.
    """

    def __call__(self, draft: OrderDraft) -> RiskDecision: ...


def _has_finite_limit(value: Any) -> bool:
    """Whether a limit is an actual number.

    ``RiskLimits.max_position_notional`` defaults to ``float("inf")``, which is
    truthy — so a naive ``if limit`` reads "no limit configured" as "a limit is
    configured". Anything non-finite or non-positive means the check does not
    apply, and a check that does not apply must not demand a price.
    """
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


@dataclass
class LimitsRiskGate:
    """Adapts the existing :class:`~atr.execution.risk.RiskEngine` to the gate protocol.

    The engine is used unchanged — this is an adapter, not a reimplementation.
    It is portfolio-coupled (it needs ``portfolio.position(symbol)`` for the
    notional and shorting checks), so the portfolio and an instrument lookup are
    injected rather than looked up here. That injection is what lets the backtest,
    paper and live paths share one gate rather than each deciding for itself.

    One thing the adapter adds, and it is a safety fix rather than a feature:
    ``RiskEngine.check_order`` prices an order as ``limit_price or last_price``,
    and when *neither* is available the notional comes out ``0.0`` — so a MARKET
    order with no limit price and no known last price passes every notional limit
    without being measured against any of them. That is the "unknown reported as
    a zero" failure this codebase is built to avoid, so when a finite notional
    limit is configured and the order cannot be priced, this gate **rejects**
    instead of letting the limit silently not apply.

    The engine's own verdict is asked first, so its reasons (kill switch, daily
    loss, symbol restriction) are reported ahead of the pricing backstop.
    """

    engine: Any  # atr.execution.risk.RiskEngine
    portfolio: Any
    instruments: Any  # Callable[[str, str], atr.core.models.Instrument]

    def __call__(self, draft: OrderDraft) -> RiskDecision:
        from atr.core.enums import OrderType, Side
        from atr.core.models import Order

        instrument = self.instruments(draft.symbol, draft.exchange)
        order = Order(
            instrument=instrument,
            side=Side.BUY if draft.side == "BUY" else Side.SELL,
            quantity=abs(float(draft.quantity)),
            order_type=OrderType(draft.order_type.upper()),
            limit_price=draft.limit_price,
            stop_price=draft.stop_price,
            tag=draft.tag,
        )
        verdict = self.engine.check_order(order, self.portfolio)
        if not verdict.allowed:
            return RiskDecision.reject(
                verdict.reason or "rejected by risk", "risk_limit"
            )

        limits = getattr(self.engine, "limits", None)
        needs_price = _has_finite_limit(
            getattr(limits, "max_order_notional", None)
        ) or _has_finite_limit(getattr(limits, "max_position_notional", None))
        if needs_price and not (draft.limit_price or self._last_price(draft)):
            # Note: `check_order` has already counted this order against the
            # daily trade limit, so a rejection here consumes one. That is the
            # pre-existing behaviour of the engine and is left alone rather than
            # reaching into it; the cost is a slightly conservative counter.
            return RiskDecision.reject(
                f"cannot price {draft.symbol}, so the notional limit cannot be "
                "evaluated — refusing rather than treating the notional as zero",
                "unpriceable_order",
            )
        return RiskDecision.ok()

    def _last_price(self, draft: OrderDraft) -> float:
        """The price the engine would use, read the same way it reads it."""
        try:
            return float(self.portfolio.position(draft.symbol).last_price or 0.0)
        except Exception:  # noqa: BLE001 - a portfolio that cannot answer is "no price"
            return 0.0


__all__ = [
    "EVENT_SOURCES",
    "ORDER_STATES",
    "PROVENANCE_FORWARD",
    "PROVENANCE_KEY",
    "RECORDED_AT_KEY",
    "TERMINAL_STATES",
    "VALID_TRANSITIONS",
    "InvalidTransition",
    "LimitsRiskGate",
    "OrderDraft",
    "RiskDecision",
    "RiskGate",
    "assert_transition",
    "can_transition",
    "elapsed_ms",
    "fill_status",
    "idempotency_key_for",
    "is_terminal",
    "next_states",
    "slippage_bps",
]
