"""Monitoring routes — ``/api/v1/monitor``.

One deployment, everything its screen needs. This sits beside
``/api/v1/paper`` rather than inside it because the two answer different
questions: ``paper`` owns the *account* (positions, cash, P&L, orders) and this
owns the *narrative* — what the deployment is doing, why it is or is not trading,
and the causal chain from a signal to a position update.

``overview`` is deliberately one request rather than six. The six views are of
one state; a screen that fetches P&L, then positions, then orders can render a
position from before a fill next to a P&L from after it, and the two disagree for
as long as the operator is looking at them.

Read-only throughout. Nothing here can start, stop or alter a deployment — a
monitoring bug must be able to misreport and never to mis-trade.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.services.monitoring import DeploymentMonitor, MonitoringError

logger = logging.getLogger("atr.api.monitor")

router = APIRouter(prefix="/api/v1/monitor", tags=["monitor"])

_READ = require_permission(Permission.ORDER_READ)


def _monitor() -> DeploymentMonitor:
    return DeploymentMonitor()


def _fail(exc: MonitoringError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": str(exc), "code": exc.code})


@router.get("/deployments/{deployment_id}")
def deployment_overview(
    deployment_id: str, principal: Principal = Depends(_READ)
) -> dict[str, Any]:
    """The whole monitoring screen for one deployment, in one read.

    Carries ``status``, ``pnl``, ``positions``, ``orders``, ``fills``,
    ``signals``, ``trades``, ``risk`` and the ``timeline``.

    ``status.trading`` is the field to read first and the one that is easy to get
    wrong: a deployment can be ``RUNNING`` and still place no orders. When it is
    false, ``not_trading_because`` says why — the rules could not be resolved, the
    session is shut, or the runner has not attached a loop yet.
    """
    try:
        return _monitor().overview(principal.user_id, deployment_id)
    except MonitoringError as exc:
        raise _fail(exc) from exc


@router.get("/deployments/{deployment_id}/status")
def deployment_status(
    deployment_id: str, principal: Principal = Depends(_READ)
) -> dict[str, Any]:
    """Just the header: lifecycle state, and whether it is actually trading."""
    try:
        return _monitor().status(principal.user_id, deployment_id)
    except MonitoringError as exc:
        raise _fail(exc) from exc


@router.get("/deployments/{deployment_id}/pnl")
def deployment_pnl(
    deployment_id: str, principal: Principal = Depends(_READ)
) -> dict[str, Any]:
    """Lifetime P&L and *today's* P&L, kept as separate figures.

    ``today_pnl`` is ``null`` when the log holds no fill before the session
    boundary — there is no earlier equity to subtract, and returning ``0`` would
    report a first day's gain as nothing.
    """
    try:
        return _monitor().pnl(principal.user_id, deployment_id)
    except MonitoringError as exc:
        raise _fail(exc) from exc


@router.get("/deployments/{deployment_id}/timeline")
def deployment_timeline(
    deployment_id: str,
    principal: Principal = Depends(_READ),
    limit: int = 300,
) -> dict[str, Any]:
    """Signal → Risk Decision → Order → Fill → Position Update, in order.

    Each entry names the stage it belongs to, so the UI renders the chain without
    inferring it from an order status, and carries the reason when the step was a
    decision — ``approved``, ``rejected`` — rather than a fact.
    """
    try:
        entries = _monitor().timeline(
            principal.user_id, deployment_id, limit=max(1, min(limit, 1000))
        )
    except MonitoringError as exc:
        raise _fail(exc) from exc
    return {
        "events": [
            {
                "ts": e.ts.isoformat(),
                "stage": e.stage,
                "symbol": e.symbol,
                "summary": e.summary,
                "outcome": e.outcome,
                "reason": e.reason,
                "order_id": e.order_id,
                "detail": e.detail,
            }
            for e in entries
        ],
        "total": len(entries),
        # The pipeline rail the UI draws, in causal order.
        "stages": ["signal", "risk", "order", "fill", "position"],
    }


@router.get("/deployments/{deployment_id}/signals")
def deployment_signals(
    deployment_id: str,
    principal: Principal = Depends(_READ),
    limit: int = 200,
) -> dict[str, Any]:
    """Signals that became orders, with the rule and reason that raised them.

    There is no separate signal table and there should not be: a signal nothing
    acted on is not a record worth keeping, and it would fill the screen with rows
    that explain nothing. What is recorded is the signal that *did* produce an
    order, on that order's ``NEW`` event.
    """
    try:
        rows = _monitor().signals(
            principal.user_id, deployment_id, limit=max(1, min(limit, 1000))
        )
    except MonitoringError as exc:
        raise _fail(exc) from exc
    return {"signals": rows, "total": len(rows)}


@router.get("/deployments/{deployment_id}/trades")
def deployment_trades(
    deployment_id: str,
    principal: Principal = Depends(_READ),
    limit: int = 200,
) -> dict[str, Any]:
    """Journaled trades.

    The journal is the only place a trade's duration, MFE and MAE are recorded —
    the order log holds fills, not episodes. So this is honestly empty until
    something journals, rather than reconstructing episodes from fills and
    presenting a guess as a record.
    """
    try:
        return _monitor().trades(
            principal.user_id, deployment_id, limit=max(1, min(limit, 1000))
        )
    except MonitoringError as exc:
        raise _fail(exc) from exc


@router.get("/deployments/{deployment_id}/risk")
def deployment_risk(
    deployment_id: str, principal: Principal = Depends(_READ)
) -> dict[str, Any]:
    """The risk state, next to the deployment's own numbers.

    The kill switch and the limits are platform-wide; they are reported alongside
    this deployment's folded book because a limit is only meaningful next to the
    figure it constrains.
    """
    try:
        return _monitor().risk(principal.user_id, deployment_id)
    except MonitoringError as exc:
        raise _fail(exc) from exc
