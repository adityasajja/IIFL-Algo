"""Kill switch, execution mode and the live-trading guards."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.config.settings import get_settings

from atr.api.legacy.common import _append_audit, _authed_client, _broker_rows

logger = logging.getLogger("atr.api")

router = APIRouter()


@router.post("/risk/kill-switch")
def kill_switch(
    engaged: bool = True,
    reason: str = "",
    principal: Principal = Depends(require_permission(Permission.RISK_CONFIGURE)),
) -> dict[str, Any]:
    """Engage or release the global kill switch. Blocks all new orders until cleared.

    Rewired onto the durable state on 2026-09-14. It used to write an in-process
    dict, which had two consequences: a restart silently re-armed trading, and
    there was no single answer to "is the switch engaged?" because each route read
    the dict itself. The switch is now stored in ``system_state`` and read through
    ``RiskStateService``, which is also where the OMS risk gate gets it — so it
    applies to every order path rather than to the routes that remembered to ask.

    A reason is now required in **both** directions. Releasing is arguably the more
    consequential of the two: it re-enables trading, and a release with no recorded
    reason is indistinguishable from someone clearing it by accident.
    """
    from atr.services.risk import RiskStateError, RiskStateService

    # The actor is who authenticated, never a query parameter: a client-supplied
    # name would let anyone write any identity into the audit trail.
    actor = principal.username

    if not reason.strip():
        raise HTTPException(
            400,
            detail={
                "detail": (
                    "changing the kill switch requires a reason — it is recorded in "
                    "the audit trail"
                ),
                "code": "reason_required",
            },
        )
    try:
        state = RiskStateService().set_kill_switch(engaged, reason=reason, actor=actor)
    except RiskStateError as exc:
        raise HTTPException(exc.status, detail={"detail": str(exc), "code": exc.code}) from exc

    _append_audit(
        actor=actor,
        action="kill_switch.engage" if engaged else "kill_switch.release",
        subject="global",
        detail=reason.strip(),
    )
    return {"kill_switch": state.kill_switch, "reason": reason.strip()}


@router.get("/risk/execution-mode")
def get_execution_mode() -> dict[str, Any]:
    """Whether orders reach the broker, or are only recorded.

    `paper` is the safe default: signals are generated and graded exactly as in
    live, the order path is exercised up to the broker boundary, but nothing is
    transmitted. This is enforced in the execution service, not merely shown here
    — a toggle that only changes a label is worse than none, because it invites
    you to trust it.
    """
    return _risk_state()


@router.post("/risk/execution-mode")
def set_execution_mode(
    mode: str,
    reason: str = "",
    principal: Principal = Depends(require_permission(Permission.EXECUTION_MODE_CHANGE)),
) -> dict[str, Any]:
    """Switch between `paper` and `live`.

    Going *live* requires an explicit reason. That is deliberate friction: the
    transition that can lose real money should cost a sentence, and the
    sentence is what shows up in the audit trail later. Coming back to paper does
    not require one — reducing risk must never be harder than taking it on.
    """
    from atr.services.risk import RiskStateError, RiskStateService

    actor = principal.username

    if mode not in {"paper", "live"}:
        raise HTTPException(400, "mode must be 'paper' or 'live'")
    if mode == "live" and not reason.strip():
        raise HTTPException(
            400,
            detail={
                "detail": (
                    "Switching to live requires a reason — it is recorded in the "
                    "audit trail"
                ),
                "code": "reason_required",
            },
        )

    previous = _risk_state()["mode"]
    try:
        RiskStateService().set_execution_mode(mode, reason=reason, actor=actor)
    except RiskStateError as exc:
        raise HTTPException(exc.status, detail={"detail": str(exc), "code": exc.code}) from exc

    _append_audit(
        actor=actor,
        action="execution_mode.change",
        subject=f"{previous} -> {mode}",
        detail=reason.strip() or None,
    )
    logger.warning(
        "execution mode %s -> %s by %s (%s)", previous, mode, actor, reason.strip() or "no reason"
    )
    return _risk_state()


@router.get("/risk/status")
def risk_status() -> dict[str, Any]:
    """Kill switch plus the limits that actually gate order placement.

    Reads the *live* settings rather than restating a config default, so the
    panel cannot drift from what the engine enforces — a risk panel showing a
    stale limit is worse than no panel.
    """
    from atr.trade_signals import load_settings

    state = _risk_state()
    settings = get_settings()
    ts = load_settings()

    # Broker-reported margin is best-effort: a dead session must not blank the
    # panel, because the kill switch is exactly what you reach for when things
    # are broken.
    margin: dict[str, Any] = {}
    margin_error: str | None = None
    try:
        client = _authed_client()
        try:
            row = _broker_rows(client.limits())
            row = row[0] if row else {}
            for key in (
                "availableMargin", "marginUtilized", "collateralValue",
                "openingCashLimit", "intradayPayinAmount", "creditForSellAmount",
                "blockedForPayoutAmount", "utilizedAmount", "net",
            ):
                if key in row:
                    margin[key] = row[key]
        finally:
            with contextlib.suppress(Exception):
                client.close()
    except Exception as exc:  # noqa: BLE001 — report, don't fail the panel
        margin_error = str(exc)

    return {
        "kill_switch": bool(state.get("kill_switch")),
        "execution_mode": str(state.get("execution_mode", "paper")),
        "env": settings.env,
        "live_orders_allowed": settings.env in {"paper", "live"},
        "limits": {
            "capital": ts.capital,
            "risk_per_trade_pct": ts.risk_per_trade_pct,
            "max_active": ts.max_active,
            "rr_ratio": ts.rr_ratio,
            "stop_method": ts.stop_method,
            "stop_atr_mult": ts.stop_atr_mult,
            "stop_pct": ts.stop_pct,
            "product": ts.product,
        },
        "margin": margin,
        "margin_error": margin_error,
    }


# ---------------------------------------------------------------------------
# Semi-automatic trade signals
# ---------------------------------------------------------------------------
# Self-learning quantitative engine
# ---------------------------------------------------------------------------


# ----------------------------------------------------------------------
def _risk_state() -> dict[str, Any]:
    """The kill switch and execution mode, read from the durable store.

    This used to return a module-level dict. Two things were wrong with that: a
    restart silently re-armed trading and dropped the platform back to paper, and
    there was no single answer to "is the kill switch engaged?" because every
    route read the dict itself — the manual order route never asked at all.

    It is now a *view* over ``atr.services.risk.RiskStateService``, which is the
    same state the OMS risk gate reads. One source of truth, so the switch applies
    everywhere rather than to the routes that remembered.

    A read failure returns the safe answer (paper, switch engaged = False is
    deliberately *not* the fallback for the switch — see below).
    """
    from atr.services.risk import RiskStateService

    try:
        snapshot = RiskStateService().snapshot()
    except Exception:  # noqa: BLE001 - an unreadable store must not 500 the dashboard
        logger.exception("could not read the risk state; reporting the safe default")
        # Paper, because an unreadable mode must never read as "live". The kill
        # switch defaults to False because `_require_live_execution` already
        # refuses to transmit in paper mode, so nothing can be sent anyway — and
        # reporting it as engaged would misrepresent what an operator did.
        return {"mode": "paper", "live": False, "paper": True,
                "changed_at": None, "changed_by": None, "reason": None}
    return {
        "mode": snapshot.execution_mode,
        "live": snapshot.live,
        "paper": not snapshot.live,
        "changed_at": snapshot.changed_at,
        "changed_by": snapshot.changed_by,
        "reason": snapshot.reason,
        # Legacy key names kept so existing readers and the dashboard do not break.
        "execution_mode": snapshot.execution_mode,
        "kill_switch": snapshot.kill_switch,
        "mode_changed_at": snapshot.changed_at,
        "mode_changed_by": snapshot.changed_by,
        "mode_reason": snapshot.reason,
    }


def _kill_switch_engaged() -> bool:
    """Whether the global kill switch is on. Read through the durable store."""
    return bool(_risk_state().get("kill_switch", False))


def _live_broker():
    """The broker that may place orders, via the shared access service.

    Delegates rather than building the client here: the construction of an
    authenticated IIFL client used to live in this module, which meant the
    reconciler — a service — would have had to reach into the transport layer for
    one. `atr.services.broker_access` owns it now, and it owns the paper/live gate
    with it, so "can this transmit?" has exactly one answer.
    """
    from atr.services.broker_access import BrokerUnavailable, live_broker

    try:
        return live_broker()
    except BrokerUnavailable as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def _order_principal(request: Request):
    """The account that owns an order placed through a legacy route.

    The legacy order routes predate accounts. An order without an owner is not
    something this platform can store — ``orders.user_id`` is a non-null foreign
    key — and it is not something it *should* store: an unattributed order is one
    nobody can be asked about, and the per-account kill switch has nothing to
    switch.

    So placing an order requires a platform account while read-only legacy routes
    stay open. This is a deliberate tightening of behaviour, recorded in
    ``docs/NEXT_STAGE_GAP_REPORT.md`` §4.2: the previous shape let an
    unauthenticated request reach the exchange with no risk check.
    """
    from atr.api.deps import optional_principal

    principal = optional_principal(request)
    if principal is None or not getattr(principal, "user_id", None):
        raise HTTPException(
            401,
            detail={
                "detail": (
                    "placing an order requires a platform account — sign in first. "
                    "Read-only routes remain available without one."
                ),
                "code": "authentication_required_for_orders",
            },
        )
    return principal


def _require_live_execution(action: str = "order") -> None:
    """Refuse to transmit orders unless the operator has switched to live.

    Called by every endpoint that can reach the broker with an order. Read-only
    paths deliberately skip this — the whole point of paper mode is that you can
    still see the market, the signals, and the queue.
    """
    store = _risk_state()
    if str(store.get("execution_mode", "paper")) != "live":
        raise HTTPException(
            403,
            f"{action} blocked: execution mode is 'paper'. "
            "Switch to live in Trading → Execution mode (a reason is required).",
        )
