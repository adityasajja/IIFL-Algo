"""Positions and the manual order endpoint."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel


from atr.api.legacy.common import _append_audit
from atr.api.legacy.risk import _live_broker, _order_principal, _require_live_execution

logger = logging.getLogger("atr.api")

router = APIRouter()


class OrderRequest(BaseModel):
    symbol: str
    exchange: str = "NSEEQ"
    quantity: int  # signed: negative = sell
    order_type: str = "MARKET"
    price: float | None = None
    product: str | None = None
    tag: str | None = None


@router.get("/positions")
def positions() -> list[dict[str, Any]]:
    broker = _live_broker()
    return [
        {
            "symbol": p.instrument.symbol,
            "exchange": p.instrument.exchange,
            "quantity": p.quantity,
            "avg_price": p.avg_price,
            "last_price": p.last_price,
            "unrealized_pnl": p.unrealized_pnl,
            "realized_pnl": p.realized_pnl,
        }
        for p in broker.positions()
    ]


@router.post("/orders")
def place_order(request: OrderRequest, http_request: Request) -> dict[str, Any]:
    """Place a manual order.

    Rewired onto the OMS on 2026-09-14. Before this, the route called
    ``broker.place_order()`` directly: no risk check, no idempotency, no event log,
    and — because it built the ``Order`` itself — no single place where an order's
    shape was decided. It now goes through the same
    :class:`~atr.services.execution.ExecutionService` as every other path.

    **Requires a platform account.** This is a deliberate tightening. An order
    needs an owner (``orders.user_id`` is a non-null foreign key), and an
    unattributed order is one nobody can be asked about. Read-only legacy routes
    stay open; the routes that can move money now require the caller to be
    identified, which is also what makes the per-account kill switch meaningful.
    """
    from atr.execution.oms import OrderDraft
    from atr.services.execution import (
        BrokerPortfolio,
        ExecutionService,
        VenueError,
        iifl_venue,
    )
    from atr.services.orders import get_order_service

    principal = _order_principal(http_request)
    _require_live_execution("Manual order")

    broker = _live_broker()
    side = "BUY" if request.quantity > 0 else "SELL"
    draft = OrderDraft(
        user_id=principal.user_id,
        symbol=request.symbol,
        side=side,
        quantity=abs(request.quantity),
        mode="LIVE",
        exchange=request.exchange,
        order_type=request.order_type,
        limit_price=request.price,
        product=request.product,
        requested_price=request.price,
        tag=request.tag,
    )
    service = ExecutionService(
        orders=get_order_service(
            portfolio=BrokerPortfolio(broker), instruments=None
        ),
        venue=iifl_venue(broker),
    )
    try:
        placed = service.place(draft)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except VenueError as exc:
        raise HTTPException(502, str(exc)) from exc

    _append_audit(
        actor=principal.username,
        action="order.place",
        subject=f"{side} {abs(request.quantity)} {request.symbol}",
        detail=f"{request.order_type} @ {request.price or 'MKT'} -> {placed.status}",
    )
    return {
        "order_id": placed.order_id,
        "broker_order_id": placed.broker_order_id,
        "status": placed.status,
        "reject_reason": placed.reject_reason,
    }
