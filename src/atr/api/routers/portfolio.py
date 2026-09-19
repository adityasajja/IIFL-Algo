"""Portfolio Control Center and Risk Policy routes — ``/api/v1/portfolio``.

Surfaces the portfolio-level risk limits, capital allocations across deployments,
sector and stock concentrations, signal conflicts, and objective strategy comparison.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.services.portfolio import PortfolioService, policy_from_dict

logger = logging.getLogger("atr.api.portfolio")

router = APIRouter(prefix="/api/v1/portfolio", tags=["portfolio"])

_READ = require_permission(Permission.RISK_READ)
_MANAGE = require_permission(Permission.RISK_CONFIGURE)


class PolicyUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_total_exposure: float | None = Field(default=None, ge=0)
    max_daily_loss: float | None = Field(default=None, ge=0)
    max_capital_per_strategy: float | None = Field(default=None, ge=0)
    max_open_positions: int | None = Field(default=None, ge=0)
    max_stock_exposure: float | None = Field(default=None, ge=0)
    max_sector_exposure_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    max_correlated_exposure_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    conflict_mode: str = Field(default="reject")
    strategy_priorities: dict[str, float] = Field(default_factory=dict)
    correlation_groups: list[list[str]] = Field(default_factory=list)
    warn_at_pct_of_limit: float = Field(default=0.80, ge=0.0, le=1.0)
    reason: str | None = Field(default=None, max_length=255)


@router.get("/control-center", dependencies=[Depends(_READ)])
def get_control_center(
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Single read returning complete Portfolio Control Center state.

    Shows capital allocation, exposure, P&L, concentrations, active limits,
    conflict descriptions, and objective strategy performance comparisons.
    """
    service = PortfolioService()
    try:
        return service.control_center(principal.user_id)
    except Exception as exc:
        logger.exception("failed to load portfolio control center for %s", principal.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate portfolio control center: {exc}",
        ) from exc


@router.get("/policy", dependencies=[Depends(_READ)])
def get_policy(
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    """Get the current portfolio risk policy for the caller."""
    service = PortfolioService()
    policy = service.get_policy(principal.user_id)
    if policy is None:
        from atr.services.portfolio import default_policy
        return {"configured": False, "policy": default_policy().as_dict()}
    return {"configured": True, "policy": policy.as_dict()}


@router.post("/policy", dependencies=[Depends(_MANAGE)])
def update_policy(
    payload: PolicyUpdatePayload,
    principal: Principal = Depends(_MANAGE),
) -> dict[str, Any]:
    """Update portfolio-level risk limits and conflict resolution rules."""
    service = PortfolioService()
    dict_payload = payload.model_dump(exclude_unset=True)
    reason = dict_payload.pop("reason", None) or "Portfolio policy updated via Control Center"
    try:
        policy = policy_from_dict(dict_payload)
        persisted = service.set_policy(
            principal.user_id,
            policy,
            actor=principal.user_id,
            reason=reason,
        )
        return {
            "status": "updated",
            "policy": persisted.as_dict(),
        }
    except Exception as exc:
        logger.exception("failed to set portfolio policy for %s", principal.user_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid portfolio policy: {exc}",
        ) from exc
