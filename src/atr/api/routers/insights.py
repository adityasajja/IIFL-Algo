"""``/api/v1/insights``: the day's read, and the switch for sending it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from atr.api.deps import require_permission
from atr.auth.models import Principal
from atr.auth.rbac import Permission
from atr.insights.service import InsightsSettings, get_insights_service

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])

_READ = require_permission(Permission.MARKET_READ)
_CONFIG = require_permission(Permission.SYSTEM_CONFIGURE)


def _client() -> Any | None:
    """A broker client when a session exists (fresh holdings), else None (saved snapshot)."""
    try:
        from atr.services.broker_access import authed_client

        return authed_client()
    except Exception:  # noqa: BLE001 - no session is a normal state, not an error
        return None


@router.get("/today")
def today(
    fresh: bool = Query(False, description="Skip the two-minute cache"),
    principal: Principal = Depends(_READ),
) -> dict[str, Any]:
    digest = get_insights_service().build(user_id=principal.user_id, client=_client(), fresh=fresh)
    return {k: v for k, v in digest.items() if not k.startswith("_")}


@router.post("/send")
def send_now(principal: Principal = Depends(_CONFIG)) -> dict[str, Any]:
    """Send today's read to Telegram now, whether or not the daily one has gone out."""
    result = get_insights_service().send(user_id=principal.user_id, client=_client(), force=True)
    return {"sent": result["sent"], "channel": result.get("channel")}


@router.get("/settings")
def get_settings_(principal: Principal = Depends(_READ)) -> dict[str, Any]:
    return get_insights_service().settings().model_dump()


@router.put("/settings")
def put_settings(body: InsightsSettings, principal: Principal = Depends(_CONFIG)) -> dict[str, Any]:
    return get_insights_service().save_settings(body).model_dump()
