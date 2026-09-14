"""Risk routes — ``/api/v1/risk``.

The risk control centre's backend: the kill switch, the execution mode, and the
limits. Reads need ``risk:read``; every write needs ``risk:configure``, which is
admin-and-above only.

Three of these actions can lose real money and so demand an explicit, non-empty
reason: engaging the kill switch, releasing it, and switching to live. The reason
is not decoration — it is stored on the state row and it is what shows up when
somebody asks why the platform did that. The *service* enforces the requirement,
not the route, so a second caller cannot skip it.

The kill switch is enforced through the OMS's risk gate, which reads this state,
so it applies to every order path rather than to the routes that remembered to
ask. That is the fix for the defect where the switch was checked on signal
execution and not on manual orders.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.services.risk import CONFIGURABLE_LIMITS, RiskStateError, RiskStateService

logger = logging.getLogger("atr.api.risk")

router = APIRouter(prefix="/api/v1/risk", tags=["risk"])

_READ = require_permission(Permission.RISK_READ)
_CONFIGURE = require_permission(Permission.RISK_CONFIGURE)


# --------------------------------------------------------------------- models
class KillSwitchRequest(BaseModel):
    engaged: bool
    reason: str = Field(min_length=1, max_length=255)


class ExecutionModeRequest(BaseModel):
    mode: str = Field(pattern="^(?i)(PAPER|LIVE)$")
    reason: str = Field(default="", max_length=255)


class LimitsRequest(BaseModel):
    """Wholesale replacement, so "clear this limit" is expressible.

    A merge cannot tell "absent" from "cleared", and those are different requests.

    ``extra="forbid"`` because silently dropping a limit the caller believes they
    set is worse than rejecting the request: the operator would walk away thinking
    a limit was in place when it was not.
    """

    model_config = ConfigDict(extra="forbid")

    max_gross_exposure: float | None = None
    max_position_notional: float | None = None
    max_position_per_symbol: float | None = None
    max_daily_loss: float | None = None
    max_daily_trades: int | None = None
    max_open_positions: int | None = None
    max_order_notional: float | None = None
    allow_short: bool = True
    allowed_symbols: list[str] | None = None
    #: ``["09:15", "15:30"]``, exchange local time.
    trading_window: list[str] | None = None


def _service() -> RiskStateService:
    return RiskStateService()


def _fail(exc: RiskStateError) -> HTTPException:
    return HTTPException(exc.status, detail={"detail": str(exc), "code": exc.code})


# --------------------------------------------------------------------- routes
@router.get("/state", dependencies=[Depends(_READ)])
def risk_state() -> dict[str, Any]:
    """The current kill switch, execution mode and limits.

    ``limits`` reports ``null`` for "no limit" rather than ``Infinity``, which is
    not valid JSON and which a JavaScript client cannot parse.
    """
    return _service().snapshot().as_dict()


@router.get("/limits/schema", dependencies=[Depends(_READ)])
def limits_schema() -> dict[str, Any]:
    """Which limits are configurable. Served so the UI cannot offer a field the
    API will reject."""
    return {"limits": list(CONFIGURABLE_LIMITS)}


@router.post("/kill-switch")
def set_kill_switch(
    body: KillSwitchRequest, principal: Principal = Depends(_CONFIGURE)
) -> dict[str, Any]:
    """Engage or release the global kill switch.

    Both directions require a reason. Releasing is arguably the more
    consequential: it re-enables trading, and a release with no recorded reason is
    indistinguishable from someone clearing it by accident.
    """
    try:
        state = _service().set_kill_switch(
            body.engaged, reason=body.reason, actor=principal.username
        )
    except RiskStateError as exc:
        raise _fail(exc) from exc
    _audit(principal, "kill_switch.engage" if body.engaged else "kill_switch.release",
           body.reason, engaged=body.engaged)
    return state.as_dict()


@router.post("/execution-mode")
def set_execution_mode(
    body: ExecutionModeRequest, principal: Principal = Depends(_CONFIGURE)
) -> dict[str, Any]:
    """Switch between ``paper`` and ``live``.

    Going live requires a reason; coming back to paper does not, because reducing
    risk should never be harder than taking it on. The asymmetry is deliberate.
    """
    try:
        state = _service().set_execution_mode(
            body.mode, reason=body.reason, actor=principal.username
        )
    except RiskStateError as exc:
        raise _fail(exc) from exc
    _audit(principal, "execution_mode.change", body.reason or "no reason given",
           mode=state.execution_mode)
    return state.as_dict()


@router.put("/limits")
def set_limits(
    body: LimitsRequest, principal: Principal = Depends(_CONFIGURE)
) -> dict[str, Any]:
    """Replace the configured limits.

    An unknown field is refused rather than ignored: silently dropping a limit the
    caller believes they set is worse than rejecting the request.
    """
    payload = body.model_dump()
    try:
        state = _service().set_limits(payload, actor=principal.username)
    except RiskStateError as exc:
        raise _fail(exc) from exc
    _audit(principal, "risk.limits.set", "limits replaced", limits=state.as_dict()["limits"])
    return state.as_dict()


def _audit(principal: Principal, action: str, reason: str, **detail: Any) -> None:
    """Write the audit row, and never let a logging failure break the action.

    The state change has already committed by the time this runs; raising here
    would report a failure for something that did happen. The extras are folded
    into the single ``detail`` field the audit sink takes, with the reason kept
    as prose so the row reads as a sentence.
    """
    try:
        from atr.audit.log import record as audit_record

        payload: dict[str, Any] = {"reason": reason}
        payload.update(detail)
        audit_record(
            action=action,
            actor=principal.username,
            subject="risk",
            detail=payload,
            user_id=principal.user_id,
        )
    except Exception:  # noqa: BLE001 - audit must not be able to break trading
        logger.exception("failed to write the audit row for %s", action)
