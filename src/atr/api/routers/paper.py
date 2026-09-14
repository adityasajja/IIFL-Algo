"""Paper trading routes — ``/api/v1/paper``.

The paper account: deployments, positions, P&L and orders. Every route goes
through :class:`~atr.services.paper.DeploymentService` and
:class:`~atr.services.paper.PaperLedger`, which fold ``order_events`` — so what
this returns is the same data the OMS wrote, not a parallel paper book that could
drift from it.

Two shapes here are honesty features rather than conveniences:

* ``complete`` / ``unpriced_symbols``. A position with no mark cannot be valued,
  and reporting it at zero would show a fabricated loss of its whole cost basis.
  The totals cover the priced positions and the response says how many were left
  out.
* ``reset`` returns a **new deployment**, because ``order_events`` is append-only
  and a paper account's history cannot be erased. See the service for the reasoning.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.services.paper import DeploymentError, DeploymentService, PaperLedger

logger = logging.getLogger("atr.api.paper")

router = APIRouter(prefix="/api/v1/paper", tags=["paper"])

_READ = require_permission(Permission.ORDER_READ)
_RUN = require_permission(Permission.ALGO_START)
_STOP = require_permission(Permission.ALGO_STOP)


# --------------------------------------------------------------------- models
class DeploymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: str = Field(min_length=1, max_length=32)
    strategy_version: int = Field(ge=1)
    capital: float = Field(gt=0)
    mode: str = Field(default="PAPER", pattern="^(?i)(PAPER|LIVE)$")
    broker_account: str | None = Field(default=None, max_length=32)
    config: dict[str, Any] | None = None


class ReasonedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Mandatory on every action that stops or restarts trading. The sentence is
    #: what shows up when somebody asks why the strategy stopped.
    reason: str = Field(min_length=1, max_length=255)


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=255)


class MarkRequest(BaseModel):
    """Optional explicit marks, for valuing without the local cache."""

    model_config = ConfigDict(extra="forbid")

    prices: dict[str, float] | None = None


class PaperOrderRequest(BaseModel):
    """An order placed into a paper deployment.

    This is the endpoint that makes the paper engine reachable. ``POST
    /api/v1/orders`` deliberately stops before transmission — creating and
    validating an order is safe and testable on its own — so a paper fill needs a
    route that names the venue, which is this one.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=64)
    side: str = Field(pattern="^(?i)(BUY|SELL)$")
    quantity: float = Field(gt=0)
    exchange: str = Field(default="NSEEQ", max_length=16)
    order_type: str = Field(default="MARKET", max_length=12)
    limit_price: float | None = Field(default=None, gt=0)
    stop_price: float | None = Field(default=None, gt=0)
    product: str | None = Field(default=None, max_length=16)
    requested_price: float | None = Field(default=None, gt=0)
    signal_id: str | None = None
    strategy_id: str | None = None
    strategy_version: int | None = None


def _service() -> DeploymentService:
    return DeploymentService()


def _ledger() -> PaperLedger:
    return PaperLedger()


def _fail(exc: DeploymentError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": str(exc), "code": exc.code})


# --------------------------------------------------------------------- routes
@router.post("/deployments", status_code=status.HTTP_201_CREATED)
def create_deployment(
    body: DeploymentCreate, principal: Principal = Depends(_RUN)
) -> dict[str, Any]:
    """Allocate capital to a strategy version.

    A deployment is what gives a paper account a starting cash and a strategy
    version, which is what makes per-strategy P&L and per-strategy risk limits
    meaningful. Without one the ledger reports zero rather than a number somebody
    chose.
    """
    try:
        return _service().create(
            principal.user_id,
            strategy_id=body.strategy_id,
            strategy_version=body.strategy_version,
            capital=body.capital,
            mode=body.mode.upper(),
            broker_account=body.broker_account,
            config=body.config,
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"detail": str(exc), "code": "invalid_deployment"},
        ) from exc


@router.get("/deployments", dependencies=[Depends(_READ)])
def list_deployments(
    principal: Principal = Depends(_READ), status_filter: str | None = None
) -> dict[str, Any]:
    rows = _service().list(principal.user_id, status=status_filter)
    return {"deployments": rows, "total": len(rows)}


@router.get("/deployments/{deployment_id}")
def get_deployment(
    deployment_id: str, principal: Principal = Depends(_READ)
) -> dict[str, Any]:
    try:
        return _service().get(principal.user_id, deployment_id)
    except DeploymentError as exc:
        raise _fail(exc) from exc


@router.post("/deployments/{deployment_id}/start")
def start_deployment(
    deployment_id: str, principal: Principal = Depends(_RUN)
) -> dict[str, Any]:
    """Start or resume. A *stopped* deployment is terminal — reset creates a new one."""
    try:
        return _service().start(principal.user_id, deployment_id)
    except DeploymentError as exc:
        raise _fail(exc) from exc


@router.post("/deployments/{deployment_id}/pause")
def pause_deployment(
    deployment_id: str,
    body: ReasonedAction,
    principal: Principal = Depends(_STOP),
) -> dict[str, Any]:
    try:
        return _service().pause(
            principal.user_id, deployment_id, reason=body.reason
        )
    except DeploymentError as exc:
        raise _fail(exc) from exc


@router.post("/deployments/{deployment_id}/stop")
def stop_deployment(
    deployment_id: str,
    body: ReasonedAction,
    principal: Principal = Depends(_STOP),
) -> dict[str, Any]:
    """Stop. ``reason`` is mandatory — an unexplained stop is not a record."""
    try:
        return _service().stop(
            principal.user_id, deployment_id, reason=body.reason
        )
    except DeploymentError as exc:
        raise _fail(exc) from exc


@router.post("/deployments/{deployment_id}/reset")
def reset_deployment(
    deployment_id: str,
    body: ResetRequest,
    principal: Principal = Depends(_RUN),
) -> dict[str, Any]:
    """Stop this deployment and return a **new** one with the same configuration.

    Not a delete. ``order_events`` is append-only, so the old account's history
    stays readable — and the response's ``reset_from`` names it, so the previous
    run's P&L is still there to compare against.
    """
    try:
        return _service().reset(
            principal.user_id, deployment_id, reason=body.reason
        )
    except DeploymentError as exc:
        raise _fail(exc) from exc


@router.get("/deployments/{deployment_id}/positions")
def deployment_positions(
    deployment_id: str,
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    try:
        return _service().positions(principal.user_id, deployment_id)
    except DeploymentError as exc:
        raise _fail(exc) from exc


@router.post("/deployments/{deployment_id}/positions")
def deployment_positions_marked(
    deployment_id: str,
    body: MarkRequest,
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """The same view with explicit marks, for valuing without the local cache."""
    try:
        return _service().positions(
            principal.user_id, deployment_id, prices=body.prices
        )
    except DeploymentError as exc:
        raise _fail(exc) from exc


@router.get("/deployments/{deployment_id}/pnl")
def deployment_pnl(
    deployment_id: str,
    principal: Principal = Depends(_READ),
    marks: bool = True,
) -> dict[str, Any]:
    """Realised, unrealised and net P&L, plus cash and exposure.

    ``complete`` is false when a held symbol could not be marked; the totals then
    cover only the priced positions and ``unpriced_symbols`` names the rest.
    """
    try:
        return _service().pnl(
            principal.user_id, deployment_id, prices=None if marks else {}
        )
    except DeploymentError as exc:
        raise _fail(exc) from exc


@router.get("/deployments/{deployment_id}/orders")
def deployment_orders(
    deployment_id: str, principal: Principal = Depends(_READ)
) -> dict[str, Any]:
    try:
        rows = _service().orders(principal.user_id, deployment_id)
    except DeploymentError as exc:
        raise _fail(exc) from exc
    return {"orders": rows, "total": len(rows)}


@router.get("/account", dependencies=[Depends(_READ)])
def paper_account(
    principal: Principal = Depends(_READ), marks: bool = True
) -> dict[str, Any]:
    """The whole paper account, across every deployment.

    Useful without a deployment too — but then ``initial_cash`` is zero, because a
    paper account with no capital allocation has no starting cash and inventing a
    default would make ``equity`` a number nobody chose.
    """
    return _ledger().snapshot(principal.user_id, prices=None if marks else {})


@router.post(
    "/deployments/{deployment_id}/orders", status_code=status.HTTP_201_CREATED
)
def place_paper_order(
    deployment_id: str,
    body: PaperOrderRequest,
    principal: Principal = Depends(require_permission(Permission.ORDER_PLACE)),
) -> dict[str, Any]:
    """Place an order into a paper deployment, and fill it against the market.

    The same pipeline as live: create → risk-check → submit → **venue**. Only the
    venue differs, which is the whole point of the design — there is no second
    strategy implementation and no parallel order model to drift.

    The risk gate is handed this deployment's **paper portfolio**, built by folding
    its own fills. That is what makes the position-level limits
    (``max_position_per_symbol``, ``max_open_positions``, gross exposure) actually
    bite on the paper path; the versioned order surface has no position book and so
    passes a flat portfolio.
    """
    from atr.execution.oms import OrderDraft, idempotency_key_for
    from atr.services import paper as paper_service
    from atr.services.execution import ExecutionService, VenueError
    from atr.services.orders import get_order_service
    from atr.services.paper import PaperLedger

    # Ownership first: a deployment that is not yours must be indistinguishable
    # from one that does not exist, before any work happens.
    try:
        deployment = _service().get(principal.user_id, deployment_id)
    except DeploymentError as exc:
        raise _fail(exc) from exc

    ledger = PaperLedger()
    portfolio = ledger.portfolio(principal.user_id, deployment_id=deployment_id)

    draft = OrderDraft(
        user_id=principal.user_id,
        symbol=body.symbol,
        side=body.side.upper(),
        quantity=body.quantity,
        mode="PAPER",
        exchange=body.exchange,
        order_type=body.order_type,
        limit_price=body.limit_price,
        stop_price=body.stop_price,
        product=body.product,
        requested_price=body.requested_price,
        deployment_id=deployment_id,
        signal_id=body.signal_id,
        strategy_id=body.strategy_id,
        strategy_version=body.strategy_version,
        tag="ATR-PAPER",
    )
    key = None
    if body.signal_id or body.strategy_id:
        key = idempotency_key_for(
            user_id=principal.user_id,
            strategy_id=body.strategy_id,
            strategy_version=body.strategy_version,
            signal_id=body.signal_id,
            symbol=body.symbol,
            side=body.side,
        )

    service = ExecutionService(
        orders=get_order_service(portfolio=portfolio),
        # Resolved per request through the service's named seam, so the price
        # source can be substituted without patching the venue's constructor.
        venue=paper_service.paper_venue(paper_service.default_price_source()),
    )
    try:
        placed = service.place(draft, idempotency_key=key)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"detail": str(exc), "code": "invalid_order"},
        ) from exc
    except VenueError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"detail": str(exc), "code": "venue_failed",
                    "order_id": exc.order_id},
        ) from exc

    return {
        "order": placed.order,
        "created": placed.created,
        "duplicate_of": placed.duplicate_of,
        "deployment_id": deployment["deployment_id"],
    }
