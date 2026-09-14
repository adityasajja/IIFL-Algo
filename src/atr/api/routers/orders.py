"""Order routes — ``/api/v1/orders``.

Every route goes through :class:`~atr.services.orders.OrderService`, which means
every order in this surface is:

* **risk-checked** — the transition to ``RISK_APPROVED`` cannot happen without a
  decision, so there is no endpoint here that reaches a broker having skipped it;
* **logged** — each transition is an ``order_events`` row, so ``GET /{id}/history``
  reconstructs what happened and who caused it;
* **idempotent when asked** — an ``Idempotency-Key`` header makes a retry return
  the original order instead of placing a second one.

Reads and writes are separately permissioned: ``order:read`` for the log,
``order:place`` to create, ``order:cancel`` to cancel. A researcher can read the
order book and still cannot send one.

Not here yet, and deliberately: **submission to a broker.** Creating and
validating an order is safe and testable on its own; transmitting it belongs to
the execution service, which is where the paper/live split lives. A route that
both validated and transmitted would be a route that could transmit without the
risk decision being visible.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.execution.oms import (
    ORDER_STATES,
    VALID_TRANSITIONS,
    InvalidTransition,
    OrderDraft,
    idempotency_key_for,
)
from atr.services.orders import OrderNotFound, OrderService, get_order_service

logger = logging.getLogger("atr.api.orders")

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])

_READ = require_permission(Permission.ORDER_READ)
_PLACE = require_permission(Permission.ORDER_PLACE)
_CANCEL = require_permission(Permission.ORDER_CANCEL)


# --------------------------------------------------------------------- models
class OrderCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=64)
    side: str = Field(pattern="^(?i)(BUY|SELL)$")
    quantity: float = Field(gt=0)
    exchange: str = Field(default="NSEEQ", max_length=16)
    asset_class: str = Field(default="EQUITY", max_length=16)
    order_type: str = Field(default="MARKET", max_length=12)
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    tif: str = Field(default="DAY", max_length=8)
    product: str | None = Field(default=None, max_length=16)
    #: The price at raise time. Optional, but without it the fill cannot be
    #: measured for slippage — and an unmeasured slippage is not zero.
    requested_price: float | None = Field(default=None, gt=0)
    mode: str = Field(default="PAPER", pattern="^(?i)(PAPER|LIVE)$")
    tag: str | None = Field(default=None, max_length=64)
    strategy_id: str | None = None
    strategy_version: int | None = None
    signal_id: str | None = None
    correlation_id: str | None = None
    deployment_id: str | None = None
    #: Free text. Required when the order is placed live, not when it is created —
    #: drafting an order changes nothing.
    reason: str | None = None


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=255)


def _service() -> OrderService:
    return get_order_service()


def _fail(exc: InvalidTransition) -> HTTPException:
    """A refused transition is a 409 with the two states named.

    Not a 400: the request is well-formed and the caller is authorised — the
    order is simply not in a state where that move is legal, and saying which
    states were involved is the only useful part of the answer.
    """
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "detail": str(exc),
            "code": "invalid_transition",
            "from_status": exc.from_status,
            "to_status": exc.to_status,
        },
    )


def _not_found() -> HTTPException:
    """404 rather than 403 — an id's existence must not leak across accounts."""
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"detail": "no such order", "code": "not_found"},
    )


# --------------------------------------------------------------------- routes
@router.get("/state-machine")
def state_machine() -> dict[str, Any]:
    """The states and the legal transitions.

    Served rather than hardcoded in the client so the UI cannot offer a button
    that the server will refuse, and so a change to the machine is a change in
    one place.
    """
    return {
        "states": list(ORDER_STATES),
        "transitions": {k: sorted(v) for k, v in VALID_TRANSITIONS.items()},
        "terminal": sorted(s for s, targets in VALID_TRANSITIONS.items() if not targets),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_order(
    body: OrderCreate,
    principal: Principal = Depends(_PLACE),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Create an order at ``NEW``. Nothing is transmitted and no risk decision is taken yet.

    An ``Idempotency-Key`` header makes this safe to retry: the same key returns
    the order that already owns it with ``created: false`` and a ``201`` rather
    than a second order.

    When no header is supplied, a key is derived **only for a signal-driven
    order** — one that names a ``signal_id`` or a ``strategy_id``. A manual order
    gets no key at all, because two people clicking "buy 10 RELIANCE" are two
    orders, and silently collapsing them would be a bug the user could not see.
    """
    service = _service()
    draft = OrderDraft(
        user_id=principal.user_id,
        symbol=body.symbol,
        side=body.side.upper(),
        quantity=body.quantity,
        mode=body.mode.upper(),
        exchange=body.exchange,
        asset_class=body.asset_class,
        order_type=body.order_type,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        tif=body.tif,
        product=body.product,
        requested_price=body.requested_price,
        deployment_id=body.deployment_id,
        strategy_id=body.strategy_id,
        strategy_version=body.strategy_version,
        signal_id=body.signal_id,
        correlation_id=body.correlation_id,
        tag=body.tag,
    )
    derived = None
    if body.signal_id or body.strategy_id:
        derived = idempotency_key_for(
            user_id=principal.user_id,
            strategy_id=body.strategy_id,
            strategy_version=body.strategy_version,
            signal_id=body.signal_id,
            symbol=body.symbol,
            side=body.side,
        )
    key = idempotency_key or derived
    try:
        opened = service.open_order(draft, idempotency_key=key)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"detail": str(exc), "code": "invalid_order"},
        ) from exc

    order = service.get(opened.order_id, principal.user_id) or {}
    return {
        "order": order,
        "created": opened.created,
        "duplicate_of": opened.duplicate_of,
    }


@router.get("")
def list_orders(
    principal: Principal = Depends(_READ),
    status_filter: str | None = Query(default=None, alias="status"),
    symbol: str | None = None,
    mode: str | None = None,
    deployment_id: str | None = None,
    correlation_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows, total = _service().list_orders(
        principal.user_id,
        status=status_filter,
        symbol=symbol,
        mode=mode,
        deployment_id=deployment_id,
        correlation_id=correlation_id,
        limit=limit,
        offset=offset,
    )
    return {"orders": rows, "total": total, "limit": limit, "offset": offset}


@router.get("/open")
def open_orders(principal: Principal = Depends(_READ)) -> dict[str, Any]:
    """Orders that are not yet in a terminal state — what reconciliation watches."""
    return {"orders": _service().open_orders(principal.user_id)}


@router.get("/{order_id}")
def get_order(order_id: str, principal: Principal = Depends(_READ)) -> dict[str, Any]:
    order = _service().get(order_id, principal.user_id)
    if order is None:
        raise _not_found()
    return order


@router.get("/{order_id}/history")
def order_history(
    order_id: str, principal: Principal = Depends(_READ)
) -> dict[str, Any]:
    """Every transition, oldest first — the lifecycle, reconstructable.

    This is the endpoint that answers "why did this trade happen?" without
    anybody having to read a log file.
    """
    try:
        events = _service().history(order_id, principal.user_id)
    except OrderNotFound as exc:
        raise _not_found() from exc
    return {
        "order_id": order_id,
        "events": events,
        "event_count": len(events),
        "final_status": events[-1]["to_status"] if events else None,
    }


@router.get("/{order_id}/fills")
def order_fills(order_id: str, principal: Principal = Depends(_READ)) -> dict[str, Any]:
    """Only the execution events, with their slippage and latency."""
    try:
        fills = _service().fill_history(order_id, principal.user_id)
    except OrderNotFound as exc:
        raise _not_found() from exc
    return {"order_id": order_id, "fills": fills, "fill_count": len(fills)}


@router.post("/{order_id}/validate")
def validate_order(
    order_id: str, principal: Principal = Depends(_PLACE)
) -> dict[str, Any]:
    """Take the risk decision. ``NEW`` → ``RISK_APPROVED`` or ``REJECTED``.

    A rejection is a ``200`` with ``status: "REJECTED"``, not an error: the
    request succeeded, and the answer is that the order may not proceed. The
    reason is on the order and in the event log.
    """
    service = _service()
    try:
        result = service.validate(order_id, principal.user_id)
    except OrderNotFound as exc:
        raise _not_found() from exc
    except InvalidTransition as exc:
        raise _fail(exc) from exc
    order = service.get(order_id, principal.user_id) or {}
    return {
        "order_id": order_id,
        "status": result,
        "allowed": result == "RISK_APPROVED",
        "reject_reason": order.get("reject_reason"),
    }


@router.post("/{order_id}/cancel")
def cancel_order(
    order_id: str,
    body: CancelRequest,
    principal: Principal = Depends(_CANCEL),
) -> dict[str, Any]:
    """Request a cancel. Moves to ``CANCEL_PENDING``, never straight to cancelled.

    The two-step shape is deliberate: the *request* is a fact worth recording even
    when the broker answers immediately, and it is the fact that is missing when
    someone later asks why a position closed early.
    """
    service = _service()
    try:
        service.request_cancel(order_id, principal.user_id, reason=body.reason)
    except OrderNotFound as exc:
        raise _not_found() from exc
    except InvalidTransition as exc:
        raise _fail(exc) from exc
    return {"order_id": order_id, "status": "CANCEL_PENDING", "reason": body.reason}


@router.post("/{order_id}/cancel/confirm")
def confirm_cancel(
    order_id: str, principal: Principal = Depends(_CANCEL)
) -> dict[str, Any]:
    """Record the broker's confirmation. ``CANCEL_PENDING`` → ``CANCELLED``."""
    service = _service()
    try:
        service.confirm_cancel(order_id, principal.user_id)
    except OrderNotFound as exc:
        raise _not_found() from exc
    except InvalidTransition as exc:
        raise _fail(exc) from exc
    return {"order_id": order_id, "status": "CANCELLED"}
